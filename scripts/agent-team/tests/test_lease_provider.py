from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import sqlite3
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast
from unittest import mock

from agent_team import recovery
from agent_team.lease import (
    Claim,
    ClockRollbackError,
    LeaseConflictError,
    LeaseError,
    ProviderBlockedError,
    ProviderCapabilities,
    ProviderEffect,
    ProviderFenceProof,
    ProviderProofError,
    ProviderReceiptError,
    ProviderStatus,
    RecoveryFloor,
    RecoverySnapshot,
    RestoreApplyResult,
    RestoreCandidateEvidence,
    RestoreIdentity,
    RestoreReplacedEvidence,
    StoreImageObservation,
    VerifiedProviderReceipt,
    _issue_provider_effect,
)
from agent_team.store import (
    CoordinationStore,
    RestoreStoreAuthority,
    StoreClosedError,
    StoreCommitUnknownError,
    StoreError,
    StoreIntegrityError,
    StoreUnavailableError,
)


class FakeClock:
    def __init__(self, now_ns: int = 100) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def set(self, now_ns: int) -> None:
        self.now_ns = now_ns


def _audit_evidence(audit_ref: str) -> str:
    return "sha256:" + hashlib.sha256(audit_ref.encode("utf-8")).hexdigest()


def _rewrite_image_metadata(image_path: Path, key: str, value: int) -> bytes:
    image = image_path.read_bytes()
    normalized = bytearray(image)
    normalized[18:20] = bytes((1, 1))
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.deserialize(bytes(normalized))
        connection.execute(
            "UPDATE store_meta SET value = ? WHERE key = ?", (value, key)
        )
        connection.commit()
        rewritten = bytearray(connection.serialize())
    finally:
        connection.close()
    rewritten[18:20] = b"\x02\x02"
    fd = os.open(str(image_path), os.O_RDWR)
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, bytes(rewritten), 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    return bytes(rewritten)


def _rewrite_image_token(image_path: Path, operation_id: str, token: int) -> bytes:
    image = image_path.read_bytes()
    normalized = bytearray(image)
    normalized[18:20] = bytes((1, 1))
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.deserialize(bytes(normalized))
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE operation_attempts SET fencing_token = ? "
            "WHERE operation_id = ? AND attempt = 1",
            (token, operation_id),
        )
        connection.execute(
            "UPDATE effect_receipts SET fencing_token = ? "
            "WHERE operation_id = ? AND attempt = 1",
            (token, operation_id),
        )
        connection.commit()
        rewritten = bytearray(connection.serialize())
    finally:
        connection.close()
    rewritten[18:20] = bytes((2, 2))
    fd = os.open(str(image_path), os.O_RDWR)
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, bytes(rewritten), 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    return bytes(rewritten)


class FakeProvider:
    capabilities = ProviderCapabilities(
        idempotency=True,
        fencing=True,
        strong_status=True,
    )

    def __init__(self, store: CoordinationStore | None = None) -> None:
        self.store = store
        self.reservations: list[tuple[str, int]] = []
        self.executions: list[str] = []
        self._high_water: dict[str, tuple[int, int, str, int]] = {}
        self._effect_ids: dict[str, str] = {}

    def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
        if self.store is not None and self.store._connection is not None:
            assert not self.store._connection.in_transaction
        operation_id = effect.operation_id
        current = self._high_water.get(effect.effect_key)
        candidate = (
            effect.lease_epoch,
            effect.fencing_token,
            effect.owner,
            effect.attempt,
        )
        if current is not None:
            current_epoch, current_token, current_owner, current_attempt = current
            if (candidate[0], candidate[1]) < (current_epoch, current_token):
                raise ProviderProofError("stale provider fence")
            if candidate[:2] == (current_epoch, current_token) and candidate[2:] != (
                current_owner,
                current_attempt,
            ):
                raise ProviderProofError("provider fence identity collision")
        self._high_water[effect.effect_key] = candidate
        self.reservations.append((operation_id, effect.fencing_token))
        return ProviderFenceProof(
            operation_id=operation_id,
            effect_key=effect.effect_key,
            provider_id=effect.provider_id,
            owner=effect.owner,
            attempt=effect.attempt,
            lease_epoch=effect.lease_epoch,
            fencing_token=effect.fencing_token,
            proof_version=1,
            proof_ref=f"proof/{effect.fencing_token}",
        )

    def execute(self, effect: ProviderEffect) -> ProviderStatus:
        assert effect.fence_proof is not None
        current = self._high_water.get(effect.effect_key)
        expected = (
            effect.lease_epoch,
            effect.fencing_token,
            effect.owner,
            effect.attempt,
        )
        if current != expected:
            raise ProviderProofError("stale provider effect")
        provider_effect_id = self._effect_ids.setdefault(
            effect.effect_key,
            f"provider-effect/{effect.effect_key}",
        )
        self.executions.append(effect.effect_key)
        status = ProviderStatus(
            operation_id=effect.operation_id,
            effect_key=effect.effect_key,
            provider_id=effect.provider_id,
            owner=effect.owner,
            attempt=effect.attempt,
            lease_epoch=effect.lease_epoch,
            fencing_token=effect.fencing_token,
            provider_effect_id=provider_effect_id,
            status="COMPLETED" if provider_effect_id is not None else "UNKNOWN",
            consistency="STRONG",
        )
        return status

    def status(self, effect: ProviderEffect) -> ProviderStatus:
        current = self._high_water.get(effect.effect_key)
        expected = (
            effect.lease_epoch,
            effect.fencing_token,
            effect.owner,
            effect.attempt,
        )
        if current != expected:
            raise ProviderProofError("stale provider status query")
        provider_effect_id = self._effect_ids.get(effect.effect_key)
        return ProviderStatus(
            operation_id=effect.operation_id,
            effect_key=effect.effect_key,
            provider_id=effect.provider_id,
            owner=effect.owner,
            attempt=effect.attempt,
            lease_epoch=effect.lease_epoch,
            fencing_token=effect.fencing_token,
            provider_effect_id=provider_effect_id,
            status="COMPLETED" if provider_effect_id is not None else "UNKNOWN",
            consistency="STRONG",
        )


class WeakProvider:
    capabilities = ProviderCapabilities(
        idempotency=True,
        fencing=False,
        strong_status=True,
    )

    def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
        raise AssertionError("a weak provider must be blocked before its call")

    def execute(self, effect: ProviderEffect) -> ProviderStatus:
        raise AssertionError("a weak provider must be blocked before its call")

    def status(self, effect: ProviderEffect) -> ProviderStatus:
        raise AssertionError("a weak provider must be blocked before its call")


class KillAfterExecuteProvider(FakeProvider):
    def execute(self, effect: ProviderEffect) -> ProviderStatus:
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("unreachable")


class KillAfterReserveProvider(FakeProvider):
    def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("unreachable")


def _reclaim_worker(
    state_root: str,
    owner: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    try:
        with CoordinationStore(Path(state_root)) as store:
            barrier.wait(timeout=10)
            store.reclaim(
                "op-race",
                owner=owner,
                provider_id="provider/test",
                effect_key="effect/op-race",
                lease_ttl_ns=20,
                now_ns=120,
            )
        results.put("claimed")
    except LeaseConflictError:
        results.put("conflict")
    except (OSError, StoreError, ValueError) as error:
        results.put(type(error).__name__)


def _kill_after_effect_prepare(state_root: str) -> None:
    store = CoordinationStore(Path(state_root))
    claim = store.claim(
        "op-kill",
        owner="owner-a",
        provider_id="provider/test",
        lease_ttl_ns=1_000_000_000,
    )
    claim = store.reserve_fence(claim, KillAfterExecuteProvider())
    store.execute_effect(claim, KillAfterExecuteProvider())
    os.kill(os.getpid(), signal.SIGKILL)


def _kill_after_fence_reservation_start(state_root: str) -> None:
    store = CoordinationStore(Path(state_root))
    claim = store.claim(
        "op-fence-kill",
        owner="owner-a",
        provider_id="provider/test",
        lease_ttl_ns=1_000_000_000,
    )
    store.reserve_fence(claim, KillAfterReserveProvider())


class LeaseProviderContractTest(unittest.TestCase):
    def _store(
        self, clock: FakeClock
    ) -> tuple[tempfile.TemporaryDirectory[str], CoordinationStore]:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-lease-")
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        state_root.mkdir(mode=0o700)
        store = CoordinationStore(state_root, clock=clock)
        store.create_intent(
            "op-1",
            effect_key="effect/op-1",
            provider_id="provider/test",
            actor="main",
            clock_ns=clock.now_ns,
        )
        return temporary, store

    def test_claim_fence_effect_receipt_and_complete_are_durable(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            provider = FakeProvider(store)
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=clock.now_ns,
            )
            self.assertEqual("FENCE_PENDING", claim.phase)
            claim = store.reserve_fence(claim, provider)
            self.assertEqual("CLAIMED", claim.phase)
            receipt = store.execute_effect(claim, provider, now_ns=101)
            operation = store.complete(receipt, now_ns=102)
            self.assertEqual("COMPLETED", operation.status)
            self.assertEqual(1, len(provider.executions))
            snapshot = store._recovery_snapshot("op-1")
            self.assertIsInstance(snapshot, RecoverySnapshot)
            self.assertIsNotNone(snapshot.verified_receipt_identity)
            with store._recovery_transaction() as transaction:
                self.assertNotIn("connection", dir(transaction))
                self.assertEqual(snapshot, transaction.snapshot("op-1"))
                self.assertIsNone(transaction.claim("op-1"))
                self.assertIsNone(transaction.effect("op-1"))
                self.assertEqual(
                    snapshot.verified_receipt_identity,
                    transaction.receipt("op-1"),
                )
                transaction.append_event(
                    snapshot,
                    kind="recover",
                    reason_code="recover",
                    actor="operator",
                    timestamp=103,
                    evidence_ref="sha256:" + "a" * 64,
                )
            self.assertEqual("recover", store.events("op-1")[-1].kind)
        finally:
            store.close()
            temporary.cleanup()

    def test_expiry_boundary_reclaim_increments_attempt_and_token(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(LeaseConflictError):
                store.reclaim(
                    claim,
                    owner="owner-b",
                    provider_id="provider/test",
                    lease_ttl_ns=20,
                    now_ns=119,
                )
            claim = store.reserve_fence(claim, FakeProvider())
            clock.set(120)
            replacement = store.reclaim(
                claim,
                owner="owner-b",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=120,
            )
            self.assertEqual(2, replacement.attempt)
            self.assertGreater(replacement.fencing_token, claim.fencing_token)
            self.assertEqual("FENCE_PENDING", replacement.phase)
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(claim, lease_ttl_ns=20, now_ns=121)
        finally:
            store.close()
            temporary.cleanup()

    def test_pending_claim_can_reclaim_at_expiry_before_reservation_starts(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(LeaseConflictError):
                store.reclaim(
                    claim,
                    owner="owner-b",
                    provider_id="provider/test",
                    lease_ttl_ns=20,
                    now_ns=119,
                )
            replacement = store.reclaim(
                claim,
                owner="owner-b",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=120,
            )
            self.assertEqual(2, replacement.attempt)
            self.assertEqual("FENCE_PENDING", replacement.phase)
            clock.set(120)
            with self.assertRaises(LeaseConflictError):
                store.reserve_fence(claim, FakeProvider())
        finally:
            store.close()
            temporary.cleanup()

    def test_recovery_floor_is_opaque_and_fences_old_epoch_claims(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            reservation = store._reserve_floor()
            floor = store._advance_floor(reservation, now_ns=101)
            self.assertEqual(1, floor.recovery_epoch)
            self.assertGreater(floor.fencing_token_floor, claim.fencing_token)
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(claim, lease_ttl_ns=20, now_ns=102)
            clock.set(103)
            store.create_intent(
                "op-after-recovery",
                effect_key="effect/op-after-recovery",
                provider_id="provider/test",
                actor="main",
                clock_ns=103,
            )
            replacement = store.claim(
                "op-after-recovery",
                owner="owner-b",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=103,
            )
            self.assertEqual(floor.recovery_epoch, replacement.lease_epoch)
            self.assertGreater(replacement.fencing_token, floor.fencing_token_floor)
            with self.assertRaises(TypeError):
                type(reservation)(  # type: ignore[call-arg]
                    recovery_epoch=floor.recovery_epoch,
                    fencing_token_floor=floor.fencing_token_floor,
                )
        finally:
            store.close()
            temporary.cleanup()

    def test_store_owned_candidate_apply_returns_typed_result_without_provider_calls(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        destination_temporary, destination_store = self._store(clock)
        source_fd = destination_fd = target_fd = None
        try:
            clock.set(101)
            destination_store.create_intent(
                "destination-only",
                effect_key="effect/destination-only",
                provider_id="provider/test",
                actor="main",
                clock_ns=101,
            )
            source_connection = store._connection
            destination_connection = destination_store._connection
            assert source_connection is not None
            assert destination_connection is not None
            source_path = Path(temporary.name) / "source-image"
            destination_path = Path(destination_temporary.name) / "destination-image"
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(destination_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(temporary.name) / "candidate-image"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            authority = RestoreStoreAuthority()
            source_observation = authority.inspect_restore(source_fd)
            destination_observation = authority.inspect_restore(destination_fd)
            self.assertIsInstance(source_observation, StoreImageObservation)
            self.assertIsInstance(destination_observation, StoreImageObservation)
            self.assertFalse(hasattr(source_observation, "connection"))
            self.assertFalse(hasattr(source_observation, "fd"))
            ledger_floor = RecoveryFloor(2, 20)
            reservation = authority.reserve_restore_floor(
                source=source_observation,
                destination=destination_observation,
                ledger_floor_lower_bound=ledger_floor,
            )
            result = authority.apply_candidate(
                source_fd,
                destination_fd,
                target_fd,
                source_observation=source_observation,
                destination_observation=destination_observation,
                ledger_floor_lower_bound=ledger_floor,
                previous_active_tombstones=(
                    RestoreIdentity("prior-op", "effect/prior"),
                ),
                reservation=reservation,
                restore_generation=1,
                actor="operator",
                timestamp=200,
            )
            self.assertIsInstance(result, RestoreApplyResult)
            with self.assertRaises(TypeError):
                RestoreApplyResult(
                    observation=result.observation,
                    tombstones=result.tombstones,
                    restore_event_count=result.restore_event_count,
                )
            self.assertGreater(result.floor.recovery_epoch, ledger_floor.recovery_epoch)
            self.assertGreater(
                result.floor.fencing_token_floor,
                ledger_floor.fencing_token_floor,
            )
            self.assertIsInstance(result.tombstones, tuple)
            self.assertEqual(1, len(result.tombstones))
            self.assertIsInstance(result.tombstones[0], RestoreIdentity)
            self.assertEqual("destination-only", result.tombstones[0].operation_id)
            self.assertEqual(
                (
                    RestoreIdentity("destination-only", "effect/destination-only"),
                    RestoreIdentity("prior-op", "effect/prior"),
                ),
                result.active_tombstones,
            )
            verified = authority.verify_candidate(target_fd, result)
            self.assertEqual(result, verified)
            self.assertFalse(hasattr(result, "connection"))
            self.assertFalse(hasattr(result, "fd"))
        finally:
            for fd in (source_fd, destination_fd, target_fd):
                if fd is not None:
                    os.close(fd)
            store.close()
            destination_store.close()
            temporary.cleanup()
            destination_temporary.cleanup()

    def test_restore_store_authority_constructs_without_opening_primary_state(
        self,
    ) -> None:
        with (
            mock.patch(
                "agent_team.store._open_state_root",
                side_effect=AssertionError("authority must not open state root"),
            ) as open_root,
            mock.patch(
                "agent_team.store.sqlite3.connect",
                side_effect=AssertionError("authority must not open SQLite"),
            ) as connect,
            mock.patch(
                "agent_team.store.os.open",
                side_effect=AssertionError("authority must not open files"),
            ) as open_file,
        ):
            authority = RestoreStoreAuthority()
            with self.assertRaises(StoreUnavailableError):
                authority.inspect_image(-1)
        self.assertIsNotNone(authority)
        open_root.assert_not_called()
        connect.assert_not_called()
        open_file.assert_not_called()
        forged = object.__new__(RestoreStoreAuthority)
        with self.assertRaises(StoreClosedError):
            forged.inspect_image(-1)
        with self.assertRaises(TypeError):
            cast(Callable[..., object], forged.apply_candidate)(
                -1,
                -1,
                -1,
                ledger_floor_lower_bound=RecoveryFloor(0, 0),
                restore_generation=1,
                actor="operator",
                timestamp=0,
            )
        forged_result = object.__new__(RestoreApplyResult)
        with self.assertRaises(StoreClosedError):
            forged.verify_candidate(-1, forged_result)
        with self.assertRaises(LeaseConflictError):
            CoordinationStore._verify_candidate_applied(-1, forged_result)

    def test_restore_expectations_use_only_handle_evidence_fields(self) -> None:
        digest = "sha256:" + "a" * 64
        ledger = recovery.RecoveryLedgerRecord(
            version=recovery.RECOVERY_LEDGER_VERSION,
            sequence=1,
            phase="RESTORE_COMMITTED",
            restore_generation=1,
            recovery_epoch=21,
            fencing_token_floor=2_002,
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
            previous_fencing_token_hwm=2,
            previous_last_clock_ns=100,
            identities=(),
            actor="operator",
            audit_ref="audit/restore",
        )
        handle = recovery._issue_restore_handle(ledger, tombstone)
        final_floor = RecoveryFloor(
            handle.recovery_epoch,
            handle.fencing_token_floor,
        )
        evidence_ref = _audit_evidence(handle.audit_ref)
        candidate = RestoreCandidateEvidence(
            restore_generation=handle.restore_generation,
            source_digest=handle.backup_digest,
            previous_primary_digest=handle.previous_primary_digest,
            candidate_digest=handle.candidate_digest,
            final_floor=final_floor,
            tombstones=tuple(
                RestoreIdentity(
                    operation_id=identity.operation_id,
                    effect_key=identity.effect_key,
                )
                for identity in handle.identities
            ),
            active_tombstones=tuple(
                RestoreIdentity(
                    operation_id=identity.operation_id,
                    effect_key=identity.effect_key,
                )
                for identity in handle.identities
            ),
            actor=handle.actor,
            evidence_ref=evidence_ref,
        )
        replaced = RestoreReplacedEvidence(
            restore_generation=handle.restore_generation,
            source_digest=handle.backup_digest,
            candidate_digest=handle.candidate_digest,
            previous_primary_digest=handle.previous_primary_digest,
            previous_recovery_epoch=handle.previous_recovery_epoch,
            previous_fencing_token_hwm=handle.previous_fencing_token_hwm,
            previous_last_clock_ns=handle.previous_last_clock_ns,
            final_floor=final_floor,
            tombstones=candidate.tombstones,
            active_tombstones=candidate.active_tombstones,
            actor=handle.actor,
            evidence_ref=evidence_ref,
        )
        self.assertEqual(handle.candidate_digest, candidate.candidate_digest)
        self.assertEqual(handle.candidate_digest, replaced.candidate_digest)
        self.assertEqual(evidence_ref, candidate.evidence_ref)
        self.assertEqual(evidence_ref, replaced.evidence_ref)
        self.assertFalse(hasattr(candidate, "source_size"))
        self.assertFalse(hasattr(candidate, "source_identity"))
        self.assertFalse(hasattr(candidate, "restore_event_count"))
        self.assertFalse(hasattr(replaced, "destination_digest"))
        self.assertFalse(hasattr(replaced, "restore_event_count"))

    def test_restore_evidence_requires_positive_generation(self) -> None:
        digest = "sha256:" + "a" * 64
        with self.assertRaises(TypeError):
            cast(Callable[..., object], RestoreCandidateEvidence)(
                source_digest=digest,
                previous_primary_digest=digest,
                candidate_digest=digest,
                final_floor=RecoveryFloor(2, 2),
                tombstones=(),
                active_tombstones=(),
                actor="operator",
                evidence_ref=digest,
            )
        with self.assertRaises(TypeError):
            cast(Callable[..., object], RestoreReplacedEvidence)(
                source_digest=digest,
                candidate_digest=digest,
                previous_primary_digest=digest,
                previous_recovery_epoch=1,
                previous_fencing_token_hwm=1,
                previous_last_clock_ns=1,
                final_floor=RecoveryFloor(2, 2),
                tombstones=(),
                active_tombstones=(),
                actor="operator",
                evidence_ref=digest,
            )
        with self.assertRaises(ValueError):
            RestoreCandidateEvidence(
                restore_generation=0,
                source_digest=digest,
                previous_primary_digest=digest,
                candidate_digest=digest,
                final_floor=RecoveryFloor(2, 2),
                tombstones=(),
                active_tombstones=(),
                actor="operator",
                evidence_ref=digest,
            )
        with self.assertRaises(ValueError):
            RestoreReplacedEvidence(
                restore_generation=0,
                source_digest=digest,
                candidate_digest=digest,
                previous_primary_digest=digest,
                previous_recovery_epoch=1,
                previous_fencing_token_hwm=1,
                previous_last_clock_ns=1,
                final_floor=RecoveryFloor(2, 2),
                tombstones=(),
                active_tombstones=(),
                actor="operator",
                evidence_ref=digest,
            )

    def test_restore_authority_apply_requires_generation(self) -> None:
        with self.assertRaises(TypeError):
            cast(
                Callable[..., object],
                RestoreStoreAuthority().apply_candidate,
            )(
                -1,
                -1,
                -1,
                ledger_floor_lower_bound=RecoveryFloor(0, 0),
                actor="operator",
                timestamp=0,
            )

    def test_candidate_fsync_uncertainty_is_commit_unknown_and_verifiable(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        destination_temporary, destination_store = self._store(clock)
        source_fd = destination_fd = target_fd = None
        try:
            source_connection = store._connection
            destination_connection = destination_store._connection
            assert source_connection is not None
            assert destination_connection is not None
            source_path = Path(temporary.name) / "source-fsync"
            destination_path = Path(destination_temporary.name) / "destination-fsync"
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(destination_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(temporary.name) / "candidate-fsync"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            authority = RestoreStoreAuthority()
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            ledger_floor = RecoveryFloor(2, 20)
            reservation = authority.reserve_restore_floor(
                source=source_observation,
                destination=destination_observation,
                ledger_floor_lower_bound=ledger_floor,
            )
            with (
                mock.patch(
                    "agent_team.store.os.fsync",
                    side_effect=OSError("injected candidate fsync failure"),
                ),
                self.assertRaises(StoreCommitUnknownError),
            ):
                authority.apply_candidate(
                    source_fd,
                    destination_fd,
                    target_fd,
                    source_observation=source_observation,
                    destination_observation=destination_observation,
                    ledger_floor_lower_bound=ledger_floor,
                    previous_active_tombstones=(),
                    reservation=reservation,
                    restore_generation=1,
                    actor="operator",
                    timestamp=200,
                    evidence_ref=_audit_evidence("audit/restore"),
                )
            candidate_image = os.pread(target_fd, os.fstat(target_fd).st_size, 0)
            self.assertGreater(len(candidate_image), 0)
            evidence = RestoreCandidateEvidence(
                restore_generation=1,
                source_digest=source_observation.digest,
                previous_primary_digest=destination_observation.digest,
                candidate_digest="sha256:"
                + hashlib.sha256(candidate_image).hexdigest(),
                final_floor=RecoveryFloor(
                    reservation.recovery_epoch,
                    reservation.fencing_token_floor,
                ),
                tombstones=(),
                active_tombstones=(),
                actor="operator",
                evidence_ref=_audit_evidence("audit/restore"),
            )
            resumed = authority.verify_candidate_evidence(
                source_fd,
                destination_fd,
                target_fd,
                evidence,
            )
            self.assertEqual(evidence.candidate_digest, resumed.digest)
        finally:
            for fd in (source_fd, destination_fd, target_fd):
                if fd is not None:
                    os.close(fd)
            store.close()
            destination_store.close()
            temporary.cleanup()
            destination_temporary.cleanup()

    def test_replaced_evidence_binds_prior_destination_high_water_marks(self) -> None:
        clock = FakeClock(100)
        source_temporary, source_store = self._store(clock)
        destination_temporary, destination_store = self._store(clock)
        source_fd = destination_fd = target_fd = primary_fd = None
        try:
            destination_store._advance_floor(
                destination_store._reserve_floor(),
                now_ns=150,
            )
            source_connection = source_store._connection
            destination_connection = destination_store._connection
            assert source_connection is not None
            assert destination_connection is not None
            source_path = Path(source_temporary.name) / "source-prior-hwm"
            destination_path = (
                Path(destination_temporary.name) / "destination-prior-hwm"
            )
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(destination_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(source_temporary.name) / "candidate-prior-hwm"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            authority = RestoreStoreAuthority()
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            self.assertLess(
                source_observation.floor.recovery_epoch,
                destination_observation.floor.recovery_epoch,
            )
            reservation = authority.reserve_restore_floor(
                source=source_observation,
                destination=destination_observation,
                ledger_floor_lower_bound=RecoveryFloor(0, 0),
            )
            applied = authority.apply_candidate(
                source_fd,
                destination_fd,
                target_fd,
                source_observation=source_observation,
                destination_observation=destination_observation,
                ledger_floor_lower_bound=RecoveryFloor(0, 0),
                previous_active_tombstones=(),
                reservation=reservation,
                restore_generation=1,
                actor="operator",
                timestamp=200,
                evidence_ref=_audit_evidence("audit/prior-hwm"),
            )
            candidate_image = os.pread(target_fd, os.fstat(target_fd).st_size, 0)
            candidate_evidence = RestoreCandidateEvidence(
                restore_generation=1,
                source_digest=source_observation.digest,
                previous_primary_digest=destination_observation.digest,
                candidate_digest="sha256:"
                + hashlib.sha256(candidate_image).hexdigest(),
                final_floor=applied.floor,
                tombstones=applied.tombstones,
                active_tombstones=applied.active_tombstones,
                actor="operator",
                evidence_ref=_audit_evidence("audit/prior-hwm"),
            )
            os.close(target_fd)
            target_fd = None
            os.close(destination_fd)
            destination_fd = None
            primary_path = Path(source_temporary.name) / "primary-prior-hwm"
            os.rename(target_path, primary_path)
            primary_fd = os.open(str(primary_path), os.O_RDONLY)
            os.unlink(destination_path)
            replaced_evidence = RestoreReplacedEvidence(
                restore_generation=candidate_evidence.restore_generation,
                source_digest=candidate_evidence.source_digest,
                candidate_digest=candidate_evidence.candidate_digest,
                previous_primary_digest=destination_observation.digest,
                previous_recovery_epoch=destination_observation.floor.recovery_epoch,
                previous_fencing_token_hwm=max(
                    destination_observation.floor.fencing_token_floor,
                    destination_observation.max_fencing_token,
                ),
                previous_last_clock_ns=destination_observation.last_clock_ns,
                final_floor=candidate_evidence.final_floor,
                tombstones=candidate_evidence.tombstones,
                active_tombstones=candidate_evidence.active_tombstones,
                actor=candidate_evidence.actor,
                evidence_ref=candidate_evidence.evidence_ref,
            )
            verified = authority.verify_replaced_evidence(
                source_fd,
                primary_fd,
                replaced_evidence,
            )
            self.assertEqual(applied.floor, verified.floor)
            with self.assertRaises(StoreIntegrityError):
                authority.verify_replaced_evidence(
                    source_fd,
                    primary_fd,
                    replace(
                        replaced_evidence,
                        previous_recovery_epoch=applied.floor.recovery_epoch,
                    ),
                )
            with self.assertRaises(StoreIntegrityError):
                authority.verify_replaced_evidence(
                    source_fd,
                    primary_fd,
                    replace(
                        replaced_evidence,
                        previous_fencing_token_hwm=applied.floor.fencing_token_floor,
                    ),
                )
            with self.assertRaises(ClockRollbackError):
                authority.verify_replaced_evidence(
                    source_fd,
                    primary_fd,
                    replace(
                        replaced_evidence,
                        previous_last_clock_ns=applied.observation.last_clock_ns + 1,
                    ),
                )
        finally:
            for fd in (source_fd, destination_fd, target_fd, primary_fd):
                if fd is not None:
                    os.close(fd)
            source_store.close()
            destination_store.close()
            source_temporary.cleanup()
            destination_temporary.cleanup()

    def test_restore_image_rejects_noncanonical_header_and_trailing_bytes(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        image_path = Path(temporary.name) / "image"
        try:
            connection = store._connection
            assert connection is not None
            image = connection.serialize()
            invalid_images: list[bytes] = []
            for write_version, read_version in ((0, 0), (0, 2), (2, 1), (1, 2)):
                invalid = bytearray(image)
                invalid[18] = write_version
                invalid[19] = read_version
                invalid_images.append(bytes(invalid))
            for offset, replacement in (
                (20, b"\x01"),
                (21, b"\x00\x20\x20"),
                (44, b"\x00\x00\x00\x00"),
                (44, b"\x00\x00\x00\x05"),
                (56, b"\x00\x00\x00\x00"),
                (56, b"\x00\x00\x00\x04"),
                (72, b"\x01" + b"\x00" * 19),
            ):
                invalid = bytearray(image)
                invalid[offset : offset + len(replacement)] = replacement
                invalid_images.append(bytes(invalid))
            invalid_images.append(image + b"junk")
            authority = RestoreStoreAuthority()
            for invalid_image in invalid_images:
                image_path.write_bytes(invalid_image)
                image_path.chmod(0o600)
                fd = os.open(str(image_path), os.O_RDONLY)
                try:
                    with self.assertRaises(StoreIntegrityError):
                        authority.inspect_image(fd)
                finally:
                    os.close(fd)
        finally:
            store.close()
            temporary.cleanup()

    def test_restore_image_rejects_clock_and_floor_high_water_regressions(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        image_path = Path(temporary.name) / "image"
        try:
            connection = store._connection
            assert connection is not None
            image_path.write_bytes(connection.serialize())
            image_path.chmod(0o600)
            raw = sqlite3.connect(str(image_path), isolation_level=None)
            try:
                raw.execute(
                    "UPDATE store_meta SET value = 0 WHERE key = 'last_clock_ns'"
                )
                raw.commit()
            finally:
                raw.close()
            fd = os.open(str(image_path), os.O_RDONLY)
            try:
                with self.assertRaises(StoreIntegrityError):
                    RestoreStoreAuthority().inspect_image(fd)
            finally:
                os.close(fd)

            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            image_path.write_bytes(connection.serialize())
            image_path.chmod(0o600)
            raw = sqlite3.connect(str(image_path), isolation_level=None)
            try:
                raw.execute(
                    "UPDATE store_meta SET value = 0 WHERE key = 'fencing_token_floor'"
                )
                raw.commit()
            finally:
                raw.close()
            fd = os.open(str(image_path), os.O_RDONLY)
            try:
                with self.assertRaises(StoreIntegrityError):
                    RestoreStoreAuthority().inspect_image(fd)
            finally:
                os.close(fd)
            self.assertGreater(claim.fencing_token, 0)
        finally:
            store.close()
            temporary.cleanup()

    def test_empty_source_with_tombstones_fails_before_candidate_commit(self) -> None:
        clock = FakeClock(0)
        source_temporary = tempfile.TemporaryDirectory(
            prefix="agent-team-lease-source-"
        )
        destination_temporary = tempfile.TemporaryDirectory(
            prefix="agent-team-lease-destination-"
        )
        source_fd = destination_fd = target_fd = None
        source_store: CoordinationStore | None = None
        destination_store: CoordinationStore | None = None
        try:
            source_root = Path(os.path.realpath(source_temporary.name)) / "state"
            destination_root = (
                Path(os.path.realpath(destination_temporary.name)) / "state"
            )
            source_root.mkdir(mode=0o700)
            destination_root.mkdir(mode=0o700)
            source_store = CoordinationStore(source_root, clock=clock)
            destination_store = CoordinationStore(destination_root, clock=clock)
            clock.set(100)
            destination_store.create_intent(
                "destination-only",
                effect_key="effect/destination-only",
                provider_id="provider/test",
                actor="main",
                clock_ns=100,
            )
            source_connection = source_store._connection
            destination_connection = destination_store._connection
            assert source_connection is not None
            assert destination_connection is not None
            source_path = Path(source_temporary.name) / "source-clock"
            destination_path = Path(destination_temporary.name) / "destination-clock"
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(destination_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(source_temporary.name) / "candidate-clock"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            authority = RestoreStoreAuthority()
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            reservation = authority.reserve_restore_floor(
                source=source_observation,
                destination=destination_observation,
                ledger_floor_lower_bound=RecoveryFloor(0, 0),
            )
            with self.assertRaises(StoreIntegrityError):
                authority.apply_candidate(
                    source_fd,
                    destination_fd,
                    target_fd,
                    source_observation=source_observation,
                    destination_observation=destination_observation,
                    ledger_floor_lower_bound=RecoveryFloor(0, 0),
                    previous_active_tombstones=(),
                    reservation=reservation,
                    restore_generation=1,
                    actor="operator",
                    timestamp=100,
                    evidence_ref=_audit_evidence("audit/destination-clock"),
                )
        finally:
            for fd in (source_fd, destination_fd, target_fd):
                if fd is not None:
                    os.close(fd)
            if source_store is not None:
                source_store.close()
            if destination_store is not None:
                destination_store.close()
            source_temporary.cleanup()
            destination_temporary.cleanup()

    def test_all_cleaned_source_with_tombstones_fails_before_candidate_commit(
        self,
    ) -> None:
        clock = FakeClock(100)
        source_temporary, source_store = self._store(clock)
        destination_temporary, destination_store = self._store(clock)
        source_fd = destination_fd = target_fd = None
        try:
            destination_store.create_intent(
                "destination-only",
                effect_key="effect/destination-only",
                provider_id="provider/test",
                actor="main",
                clock_ns=100,
            )
            source_connection = source_store._connection
            destination_connection = destination_store._connection
            assert source_connection is not None
            assert destination_connection is not None
            cleaned_claim = source_store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            source_store.reserve_fence(cleaned_claim, FakeProvider())
            source_connection.execute(
                "UPDATE operations SET status = 'CLEANED' WHERE operation_id = 'op-1'"
            )
            source_path = Path(source_temporary.name) / "cleaned-source"
            destination_path = Path(destination_temporary.name) / "cleaned-destination"
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(destination_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(source_temporary.name) / "cleaned-candidate"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            authority = RestoreStoreAuthority()
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            reservation = authority.reserve_restore_floor(
                source_observation,
                destination_observation,
                RecoveryFloor(0, 0),
            )
            with self.assertRaises(StoreIntegrityError):
                authority.apply_candidate(
                    source_fd,
                    destination_fd,
                    target_fd,
                    source_observation=source_observation,
                    destination_observation=destination_observation,
                    ledger_floor_lower_bound=RecoveryFloor(0, 0),
                    previous_active_tombstones=(),
                    reservation=reservation,
                    restore_generation=1,
                    actor="operator",
                    timestamp=101,
                    evidence_ref=_audit_evidence("audit/cleaned"),
                )
            self.assertEqual(0, os.fstat(target_fd).st_size)
        finally:
            for fd in (source_fd, destination_fd, target_fd):
                if fd is not None:
                    os.close(fd)
            source_store.close()
            destination_store.close()
            source_temporary.cleanup()
            destination_temporary.cleanup()

    def test_all_cleaned_source_with_prior_active_union_fails_before_candidate_commit(
        self,
    ) -> None:
        clock = FakeClock(100)
        source_temporary, source_store = self._store(clock)
        destination_temporary, destination_store = self._store(clock)
        source_fd = destination_fd = target_fd = None
        try:
            source_connection = source_store._connection
            destination_connection = destination_store._connection
            assert source_connection is not None
            assert destination_connection is not None
            cleaned_claim = source_store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            source_store.reserve_fence(cleaned_claim, FakeProvider())
            source_connection.execute(
                "UPDATE operations SET status = 'CLEANED' WHERE operation_id = 'op-1'"
            )
            source_path = Path(source_temporary.name) / "cleaned-prior-source"
            destination_path = (
                Path(destination_temporary.name) / "cleaned-prior-destination"
            )
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(destination_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(source_temporary.name) / "cleaned-prior-candidate"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            authority = RestoreStoreAuthority()
            source_observation = authority.inspect_image(source_fd)
            destination_observation = authority.inspect_image(destination_fd)
            reservation = authority.reserve_restore_floor(
                source_observation,
                destination_observation,
                RecoveryFloor(0, 0),
            )
            with self.assertRaises(StoreIntegrityError):
                authority.apply_candidate(
                    source_fd,
                    destination_fd,
                    target_fd,
                    source_observation=source_observation,
                    destination_observation=destination_observation,
                    ledger_floor_lower_bound=RecoveryFloor(0, 0),
                    previous_active_tombstones=(
                        RestoreIdentity("prior-op", "effect/prior"),
                    ),
                    reservation=reservation,
                    restore_generation=2,
                    actor="operator",
                    timestamp=101,
                    evidence_ref=_audit_evidence("audit/cleaned-prior"),
                )
            with self.assertRaises(StoreIntegrityError):
                authority.apply_candidate(
                    source_fd,
                    destination_fd,
                    target_fd,
                    source_observation=source_observation,
                    destination_observation=destination_observation,
                    ledger_floor_lower_bound=RecoveryFloor(0, 0),
                    previous_active_tombstones=(
                        RestoreIdentity("op-1", "effect/op-1"),
                    ),
                    reservation=reservation,
                    restore_generation=2,
                    actor="operator",
                    timestamp=101,
                    evidence_ref=_audit_evidence("audit/cleaned-source-collision"),
                )
            self.assertEqual(0, os.fstat(target_fd).st_size)
        finally:
            for fd in (source_fd, destination_fd, target_fd):
                if fd is not None:
                    os.close(fd)
            source_store.close()
            destination_store.close()
            source_temporary.cleanup()
            destination_temporary.cleanup()

    def test_restore_invalidated_active_identity_rejects_old_mutations(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            clock.set(100)
            store.create_intent(
                "op-heartbeat",
                effect_key="effect/op-heartbeat",
                provider_id="provider/test",
                actor="main",
                clock_ns=100,
            )
            heartbeat_claim = store.claim(
                "op-heartbeat",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            heartbeat_claim = store.reserve_fence(heartbeat_claim, FakeProvider())

            store.create_intent(
                "op-effect",
                effect_key="effect/op-effect",
                provider_id="provider/test",
                actor="main",
                clock_ns=101,
            )
            effect_claim = store.claim(
                "op-effect",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=101,
            )
            effect_provider = FakeProvider()
            clock.set(101)
            effect_claim = store.reserve_fence(effect_claim, effect_provider)

            store.create_intent(
                "op-reclaim",
                effect_key="effect/op-reclaim",
                provider_id="provider/test",
                actor="main",
                clock_ns=102,
            )
            store.claim(
                "op-reclaim",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=102,
            )

            store.create_intent(
                "op-complete",
                effect_key="effect/op-complete",
                provider_id="provider/test",
                actor="main",
                clock_ns=103,
            )
            complete_claim = store.claim(
                "op-complete",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=103,
            )
            complete_provider = FakeProvider()
            clock.set(103)
            complete_claim = store.reserve_fence(complete_claim, complete_provider)
            complete_receipt = store.execute_effect(
                complete_claim,
                complete_provider,
                now_ns=104,
            )

            connection = store._connection
            assert connection is not None
            connection.execute(
                "UPDATE store_meta SET value = 1 WHERE key = 'recovery_epoch'"
            )
            connection.execute(
                "UPDATE operations SET recovery_epoch = 1 WHERE current_attempt > 0"
            )

            with self.assertRaises(LeaseConflictError):
                store.heartbeat(heartbeat_claim, lease_ttl_ns=20, now_ns=105)
            with self.assertRaises(LeaseConflictError):
                store._begin_effect(effect_claim, now_ns=105)
            with self.assertRaises(LeaseConflictError):
                store.reclaim(
                    "op-reclaim",
                    owner="owner-b",
                    provider_id="provider/test",
                    effect_key="effect/op-reclaim",
                    lease_ttl_ns=20,
                    now_ns=122,
                )
            with self.assertRaises(LeaseConflictError):
                store.complete(complete_receipt, now_ns=105)
            self.assertEqual([], effect_provider.executions)
            self.assertEqual(["effect/op-complete"], complete_provider.executions)
        finally:
            store.close()
            temporary.cleanup()

    def test_candidate_apply_preserves_every_status_and_cleans_only_global_floor(
        self,
    ) -> None:
        clock = FakeClock(100)
        temporary, store = self._store(clock)
        source_fd = destination_fd = target_fd = primary_fd = None
        try:
            source_statuses: dict[str, RecoverySnapshot] = {}

            def create_intent(operation_id: str, at: int) -> None:
                clock.set(at)
                store.create_intent(
                    operation_id,
                    effect_key=f"effect/{operation_id}",
                    provider_id="provider/test",
                    actor="main",
                    clock_ns=at,
                )

            def claim(operation_id: str, at: int) -> Claim:
                return store.claim(
                    operation_id,
                    owner="owner-a",
                    provider_id="provider/test",
                    lease_ttl_ns=20,
                    now_ns=at,
                )

            source_statuses["INTENT"] = store._recovery_snapshot("op-1")

            create_intent("op-pending", 101)
            claim("op-pending", 101)
            source_statuses["FENCE_PENDING"] = store._recovery_snapshot("op-pending")

            create_intent("op-reservation", 102)
            reservation_claim = claim("op-reservation", 102)
            clock.set(102)
            store._begin_fence_reservation(reservation_claim, now_ns=102)
            source_statuses["FENCE_RESERVATION_STARTED"] = store._recovery_snapshot(
                "op-reservation"
            )

            create_intent("op-claimed", 103)
            claimed = claim("op-claimed", 103)
            claimed_provider = FakeProvider()
            clock.set(103)
            store.reserve_fence(claimed, claimed_provider)
            source_statuses["CLAIMED"] = store._recovery_snapshot("op-claimed")

            create_intent("op-prepared", 104)
            prepared = claim("op-prepared", 104)
            prepared_provider = FakeProvider()
            clock.set(104)
            prepared = store.reserve_fence(prepared, prepared_provider)
            store._begin_effect(prepared, now_ns=104)
            source_statuses["EFFECT_PREPARED"] = store._recovery_snapshot("op-prepared")

            create_intent("op-unknown-effect", 105)
            unknown_effect_claim = claim("op-unknown-effect", 105)
            unknown_provider = FakeProvider()
            clock.set(105)
            unknown_effect_claim = store.reserve_fence(
                unknown_effect_claim,
                unknown_provider,
            )
            store._mark_unknown_effect(unknown_effect_claim, now_ns=105)
            source_statuses["UNKNOWN_EFFECT"] = store._recovery_snapshot(
                "op-unknown-effect"
            )

            create_intent("op-unknown", 106)
            unknown_claim = claim("op-unknown", 106)
            unknown_status_provider = FakeProvider()
            clock.set(106)
            unknown_claim = store.reserve_fence(unknown_claim, unknown_status_provider)
            unknown_effect = store._begin_effect(unknown_claim, now_ns=106)
            store._mark_unknown_effect(unknown_effect, now_ns=106)
            connection = store._connection
            assert connection is not None
            connection.execute(
                "UPDATE operations SET status = 'UNKNOWN' "
                "WHERE operation_id = 'op-unknown'"
            )
            source_statuses["UNKNOWN"] = store._recovery_snapshot("op-unknown")

            create_intent("op-receipted", 107)
            receipted_claim = claim("op-receipted", 107)
            receipted_provider = FakeProvider()
            clock.set(107)
            receipted_claim = store.reserve_fence(
                receipted_claim,
                receipted_provider,
            )
            store.execute_effect(
                receipted_claim,
                receipted_provider,
                now_ns=107,
            )
            source_statuses["RECEIPTED"] = store._recovery_snapshot("op-receipted")

            create_intent("op-completed", 108)
            completed_claim = claim("op-completed", 108)
            completed_provider = FakeProvider()
            clock.set(108)
            completed_claim = store.reserve_fence(
                completed_claim,
                completed_provider,
            )
            completed = store.execute_effect(
                completed_claim,
                completed_provider,
                now_ns=108,
            )
            store.complete(completed, now_ns=108)
            source_statuses["COMPLETED"] = store._recovery_snapshot("op-completed")

            create_intent("op-cleaned", 109)
            cleaned = claim("op-cleaned", 109)
            cleaned_provider = FakeProvider()
            clock.set(109)
            store.reserve_fence(cleaned, cleaned_provider)
            connection.execute(
                "UPDATE operations SET status = 'CLEANED' "
                "WHERE operation_id = 'op-cleaned'"
            )
            source_statuses["CLEANED"] = store._recovery_snapshot("op-cleaned")

            source_connection = store._connection
            assert source_connection is not None
            source_path = Path(temporary.name) / "matrix-source"
            destination_path = Path(temporary.name) / "matrix-destination"
            source_path.write_bytes(source_connection.serialize())
            destination_path.write_bytes(source_connection.serialize())
            source_path.chmod(0o600)
            destination_path.chmod(0o600)
            target_path = Path(temporary.name) / "matrix-candidate"
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            target_fd = os.open(
                str(target_path),
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            source_observation = store._inspect_image_fd(source_fd)
            destination_observation = store._inspect_image_fd(destination_fd)
            ledger_floor = RecoveryFloor(20, 2_000)
            reservation = store._reserve_restore_floor(
                source_observation,
                destination_observation,
                ledger_floor,
            )
            result = store._apply_restore_candidate(
                source_fd,
                destination_fd,
                target_fd,
                source_observation=source_observation,
                destination_observation=destination_observation,
                ledger_floor_lower_bound=ledger_floor,
                previous_active_tombstones=(),
                reservation=reservation,
                restore_generation=1,
                actor="operator",
                timestamp=300,
                evidence_ref=_audit_evidence("audit/restore"),
            )
            final_by_status = {
                snapshot.status: snapshot for snapshot in result.observation.operations
            }
            self.assertEqual(set(source_statuses), set(final_by_status))
            self.assertEqual(
                source_statuses["CLEANED"],
                final_by_status["CLEANED"],
            )
            for status in (
                "INTENT",
                "FENCE_PENDING",
                "FENCE_RESERVATION_STARTED",
                "CLAIMED",
                "EFFECT_PREPARED",
                "UNKNOWN_EFFECT",
                "UNKNOWN",
                "COMPLETED",
            ):
                self.assertEqual(
                    result.floor.recovery_epoch,
                    final_by_status[status].recovery_epoch,
                )
            self.assertEqual(
                result.floor.recovery_epoch,
                final_by_status["RECEIPTED"].recovery_epoch,
            )
            self.assertEqual(
                result.floor.recovery_epoch,
                final_by_status["RECEIPTED"].lease_epoch,
            )
            self.assertGreater(
                final_by_status["RECEIPTED"].fencing_token,
                reservation.fencing_token_floor,
            )
            self.assertEqual(9, result.restore_event_count)
            self.assertEqual(0, len(result.tombstones))
            self.assertEqual(result, store._verify_candidate_applied(target_fd, result))
            candidate_image = os.pread(target_fd, os.fstat(target_fd).st_size, 0)
            durable_evidence = RestoreCandidateEvidence(
                restore_generation=1,
                source_digest=source_observation.digest,
                previous_primary_digest=destination_observation.digest,
                candidate_digest="sha256:"
                + hashlib.sha256(candidate_image).hexdigest(),
                final_floor=RecoveryFloor(21, 2_002),
                tombstones=(),
                active_tombstones=(),
                actor="operator",
                evidence_ref=_audit_evidence("audit/restore"),
            )
            resumed = RestoreStoreAuthority().verify_candidate_evidence(
                source_fd,
                destination_fd,
                target_fd,
                durable_evidence,
            )
            self.assertEqual(result, resumed)
            original_candidate_image = candidate_image
            forged_candidate_image = _rewrite_image_token(
                target_path,
                "op-receipted",
                reservation.fencing_token_floor,
            )
            with self.assertRaises(StoreIntegrityError):
                RestoreStoreAuthority().verify_candidate_evidence(
                    source_fd,
                    destination_fd,
                    target_fd,
                    replace(
                        durable_evidence,
                        candidate_digest="sha256:"
                        + hashlib.sha256(forged_candidate_image).hexdigest(),
                    ),
                )
            restore_fd = os.open(str(target_path), os.O_RDWR)
            try:
                os.ftruncate(restore_fd, 0)
                os.pwrite(restore_fd, original_candidate_image, 0)
                os.fsync(restore_fd)
            finally:
                os.close(restore_fd)
            with self.assertRaises(StoreIntegrityError):
                RestoreStoreAuthority().verify_candidate_evidence(
                    source_fd,
                    destination_fd,
                    target_fd,
                    replace(
                        durable_evidence,
                        candidate_digest="sha256:" + "0" * 64,
                    ),
                )
            with self.assertRaises(StoreIntegrityError):
                RestoreStoreAuthority().verify_candidate_evidence(
                    source_fd,
                    destination_fd,
                    target_fd,
                    replace(
                        durable_evidence,
                        source_digest="sha256:" + "0" * 64,
                    ),
                )
            source_bytes = os.pread(source_fd, os.fstat(source_fd).st_size, 0)
            source_mutator = os.open(str(source_path), os.O_RDWR)
            try:
                os.pwrite(source_mutator, b"x", len(source_bytes) - 1)
                os.fsync(source_mutator)
            finally:
                os.close(source_mutator)
            with self.assertRaises(StoreIntegrityError):
                RestoreStoreAuthority().verify_candidate_evidence(
                    source_fd,
                    destination_fd,
                    target_fd,
                    durable_evidence,
                )
            source_mutator = os.open(str(source_path), os.O_RDWR)
            try:
                os.pwrite(source_mutator, source_bytes[-1:], len(source_bytes) - 1)
                os.fsync(source_mutator)
            finally:
                os.close(source_mutator)
            destination_bytes = os.pread(
                destination_fd,
                os.fstat(destination_fd).st_size,
                0,
            )
            destination_mutator = os.open(str(destination_path), os.O_RDWR)
            try:
                os.pwrite(destination_mutator, b"x", len(destination_bytes) - 1)
                os.fsync(destination_mutator)
            finally:
                os.close(destination_mutator)
            with self.assertRaises(StoreIntegrityError):
                RestoreStoreAuthority().verify_candidate_evidence(
                    source_fd,
                    destination_fd,
                    target_fd,
                    durable_evidence,
                )
            destination_mutator = os.open(str(destination_path), os.O_RDWR)
            try:
                os.pwrite(
                    destination_mutator,
                    destination_bytes[-1:],
                    len(destination_bytes) - 1,
                )
                os.fsync(destination_mutator)
            finally:
                os.close(destination_mutator)
            primary_path = Path(temporary.name) / "replaced-primary"
            os.close(destination_fd)
            destination_fd = None
            os.rename(target_path, primary_path)
            target_fd = None
            primary_fd = os.open(str(primary_path), os.O_RDONLY)
            os.unlink(destination_path)
            replaced_evidence = RestoreReplacedEvidence(
                restore_generation=durable_evidence.restore_generation,
                source_digest=durable_evidence.source_digest,
                candidate_digest=durable_evidence.candidate_digest,
                previous_primary_digest=destination_observation.digest,
                previous_recovery_epoch=destination_observation.floor.recovery_epoch,
                previous_fencing_token_hwm=max(
                    destination_observation.floor.fencing_token_floor,
                    destination_observation.max_fencing_token,
                ),
                previous_last_clock_ns=destination_observation.last_clock_ns,
                final_floor=durable_evidence.final_floor,
                tombstones=durable_evidence.tombstones,
                active_tombstones=durable_evidence.active_tombstones,
                actor=durable_evidence.actor,
                evidence_ref=durable_evidence.evidence_ref,
            )
            replaced = RestoreStoreAuthority().verify_replaced_evidence(
                source_fd,
                primary_fd,
                replaced_evidence,
            )
            self.assertEqual(9, replaced.restore_event_count)
            with self.assertRaises(StoreIntegrityError):
                RestoreStoreAuthority().verify_replaced_evidence(
                    source_fd,
                    primary_fd,
                    replace(
                        replaced_evidence,
                        tombstones=(RestoreIdentity("forged-op", "effect/forged"),),
                        active_tombstones=(
                            RestoreIdentity("forged-op", "effect/forged"),
                        ),
                    ),
                )
            self.assertEqual(
                replaced,
                RestoreStoreAuthority().verify_replaced_evidence(
                    source_fd,
                    primary_fd,
                    replaced_evidence,
                ),
            )
        finally:
            for fd in (source_fd, destination_fd, target_fd, primary_fd):
                if fd is not None:
                    os.close(fd)
            store.close()
            temporary.cleanup()

    def test_floor_and_typed_rebase_share_one_transaction(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            store.create_intent(
                "op-receipt",
                effect_key="effect/op-receipt",
                provider_id="provider/test",
                actor="main",
                clock_ns=101,
            )
            clock.set(101)
            receipt_claim = store.claim(
                "op-receipt",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=101,
            )
            receipt_provider = FakeProvider()
            receipt_claim = store.reserve_fence(receipt_claim, receipt_provider)
            old_receipt = store.execute_effect(
                receipt_claim,
                receipt_provider,
                now_ns=102,
            )
            clock.set(103)
            store.create_intent(
                "op-completed",
                effect_key="effect/op-completed",
                provider_id="provider/test",
                actor="main",
                clock_ns=103,
            )
            completed_claim = store.claim(
                "op-completed",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=103,
            )
            completed_provider = FakeProvider()
            completed_claim = store.reserve_fence(
                completed_claim,
                completed_provider,
            )
            completed_receipt = store.execute_effect(
                completed_claim,
                completed_provider,
                now_ns=104,
            )
            store.complete(completed_receipt, now_ns=105)
            clock.set(106)
            store.create_intent(
                "op-cleaned",
                effect_key="effect/op-cleaned",
                provider_id="provider/test",
                actor="main",
                clock_ns=106,
            )
            cleaned_claim = store.claim(
                "op-cleaned",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=106,
            )
            store.reserve_fence(cleaned_claim, FakeProvider())
            with store._lease_write_transaction() as connection:
                connection.execute(
                    """
                    UPDATE operations SET status = 'CLEANED', updated_ns = ?
                    WHERE operation_id = 'op-cleaned' AND current_attempt = 1
                    """,
                    (107,),
                )
            clock.set(107)
            snapshots = {
                operation_id: store._recovery_snapshot(operation_id)
                for operation_id in (
                    "op-1",
                    "op-receipt",
                    "op-completed",
                    "op-cleaned",
                )
            }
            cleaned_before = snapshots["op-cleaned"]
            cleaned_events_before = store.events("op-cleaned")
            reservation = store._reserve_floor()
            with store._recovery_transaction() as transaction:
                floor = transaction.advance_floor(reservation, timestamp=108)
                intent_snapshot = transaction.rebase(
                    snapshots["op-1"],
                    mode="INTENT",
                    actor="operator",
                    timestamp=109,
                )
                receipt_snapshot = transaction.rebase(
                    snapshots["op-receipt"],
                    mode="RECEIPTED",
                    actor="operator",
                    timestamp=110,
                )
                completed_snapshot = transaction.rebase(
                    snapshots["op-completed"],
                    mode="COMPLETED",
                    actor="operator",
                    timestamp=111,
                )
            self.assertEqual(1, floor.recovery_epoch)
            self.assertEqual(1, intent_snapshot.recovery_epoch)
            self.assertEqual("RECEIPTED", receipt_snapshot.status)
            self.assertEqual(1, receipt_snapshot.recovery_epoch)
            self.assertEqual("COMPLETED", completed_snapshot.status)
            cleaned_after = store._recovery_snapshot("op-cleaned")
            self.assertEqual(cleaned_before, cleaned_after)
            self.assertEqual(cleaned_events_before, store.events("op-cleaned"))
            rebound = store._rehydrate_receipt("op-receipt")
            assert rebound is not None
            self.assertEqual(old_receipt.provider_effect_id, rebound.provider_effect_id)
            self.assertEqual(old_receipt.proof_ref, rebound.proof_ref)
            self.assertEqual(floor.recovery_epoch, rebound.lease_epoch)
            self.assertGreater(rebound.fencing_token, floor.fencing_token_floor)
            with self.assertRaises(LeaseConflictError):
                store.complete(old_receipt, now_ns=113)
            self.assertEqual(
                "COMPLETED",
                store.complete(rebound, now_ns=114).status,
            )
            new_claim = store.claim(
                "op-1",
                owner="owner-b",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=114,
            )
            self.assertEqual(floor.recovery_epoch, new_claim.lease_epoch)
            self.assertGreater(new_claim.fencing_token, rebound.fencing_token)
        finally:
            store.close()
            temporary.cleanup()

    def test_weak_provider_is_blocked_without_local_token_fallback(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(ProviderBlockedError):
                store.reserve_fence(claim, WeakProvider())
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("FENCE_PENDING", operation.status)
        finally:
            store.close()
            temporary.cleanup()

    def test_forged_receipt_and_unknown_outcome_do_not_complete_or_retry(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            provider = FakeProvider()
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            with self.assertRaises(TypeError):
                ProviderEffect(
                    operation_id=claim.operation_id,
                    effect_key=claim.effect_key,
                    provider_id=claim.provider_id,
                    owner=claim.owner,
                    attempt=claim.attempt,
                    lease_epoch=claim.lease_epoch,
                    fencing_token=claim.fencing_token,
                )
            with self.assertRaises((TypeError, ValueError)):
                VerifiedProviderReceipt(
                    operation_id=claim.operation_id,
                    effect_key=claim.effect_key,
                    provider_id=claim.provider_id,
                    owner=claim.owner,
                    attempt=claim.attempt,
                    lease_epoch=claim.lease_epoch,
                    fencing_token=claim.fencing_token,
                    provider_effect_id="provider-effect/forged",
                    provider_status="COMPLETED",
                    proof_version=1,
                    proof_ref="proof/forged",
                )
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("CLAIMED", operation.status)
            self.assertEqual([], provider.executions)
        finally:
            store.close()
            temporary.cleanup()

    def test_clock_rollback_and_mutable_claims_are_rejected(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=101,
            )
            clock.set(101)
            claim = store.reserve_fence(claim, FakeProvider())
            with self.assertRaises(ClockRollbackError):
                store.heartbeat(claim, lease_ttl_ns=20, now_ns=100)
            with self.assertRaises(FrozenInstanceError):
                claim.owner = "owner-b"  # type: ignore[misc]
        finally:
            store.close()
            temporary.cleanup()

    def test_heartbeat_rejects_a_claim_with_a_forged_stored_proof(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, FakeProvider())
            assert claim.fence_proof is not None
            forged_proof = replace(claim.fence_proof, proof_ref="proof/forged")
            forged_claim = replace(claim, fence_proof=forged_proof)
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(forged_claim, lease_ttl_ns=20, now_ns=101)
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("CLAIMED", operation.status)
        finally:
            store.close()
            temporary.cleanup()

    def test_default_clock_rollback_uses_one_effective_timestamp_everywhere(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-lease-clock-")
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        state_root.mkdir(mode=0o700)
        store = CoordinationStore(state_root)
        try:
            store.create_intent(
                "op-clock",
                effect_key="effect/op-clock",
                provider_id="provider/test",
                actor="main",
            )
            durable = store._last_clock_ns
            store._clock = lambda: 0
            claim = store.claim(
                "op-clock",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
            )
            self.assertEqual(durable, claim.lease_heartbeat_ns)
            self.assertEqual(durable + 20, claim.lease_expires_ns)
            operation = store.operation("op-clock")
            assert operation is not None
            self.assertEqual(durable, operation.updated_ns)
            self.assertEqual(durable, store.events("op-clock")[-1].clock_ns)
        finally:
            store.close()
            temporary.cleanup()

    def test_reopen_fences_an_effect_left_prepared_by_a_crashed_process(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, FakeProvider())
            effect = store._begin_effect(claim, now_ns=101)
            store.close()
            reopened = CoordinationStore(
                Path(os.path.realpath(temporary.name)) / "state"
            )
            try:
                operation = reopened.operation("op-1")
                assert operation is not None
                self.assertEqual("EFFECT_PREPARED", operation.status)
                with self.assertRaises(LeaseConflictError):
                    reopened._begin_effect(claim)
                self.assertEqual(effect.effect_key, "effect/op-1")
                snapshot = reopened._recovery_snapshot("op-1")
                with reopened._recovery_transaction() as transaction:
                    transaction.mark_prepared_unknown(
                        snapshot,
                        actor="owner-a",
                        timestamp=snapshot.updated_ns + 1,
                    )
                operation = reopened.operation("op-1")
                assert operation is not None
                self.assertEqual("UNKNOWN_EFFECT", operation.status)
            finally:
                reopened.close()
        finally:
            store.close()
            temporary.cleanup()

    def test_malformed_prepared_owner_reopen_is_stable_and_fileset_invariant(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            provider = FakeProvider()
            claim = store.reserve_fence(claim, provider)
            store._begin_effect(claim, now_ns=101)
            clock.set(101)
            store.close()
            database = state_root / "coordination.sqlite3"
            before_bytes = database.read_bytes()
            connection = sqlite3.connect(str(database), isolation_level=None)
            try:
                connection.execute(
                    "UPDATE operation_attempts SET owner = 'prompt text' "
                    "WHERE operation_id = 'op-1' AND attempt = 1"
                )
            finally:
                connection.close()
            malformed_bytes = database.read_bytes()
            malformed_files = tuple(sorted(path.name for path in state_root.iterdir()))
            with self.assertRaises(StoreIntegrityError):
                CoordinationStore(state_root, clock=clock)
            self.assertEqual(malformed_bytes, database.read_bytes())
            self.assertEqual(
                malformed_files,
                tuple(sorted(path.name for path in state_root.iterdir())),
            )
            self.assertNotEqual(before_bytes, malformed_bytes)
        finally:
            store.close()
            temporary.cleanup()

    def test_recovery_snapshot_malformed_owner_is_a_store_integrity_error(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            connection = store._connection
            assert connection is not None
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE operation_attempts SET owner = 'prompt text' "
                "WHERE operation_id = 'op-1' AND attempt = 0"
            )
            with self.assertRaises(StoreIntegrityError):
                store._recovery_snapshot("op-1")
        finally:
            store.close()
            temporary.cleanup()

    def test_recovery_snapshot_query_error_is_a_store_integrity_error(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            connection = store._connection
            assert connection is not None
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE effect_receipts")
            with self.assertRaises(StoreIntegrityError):
                store._recovery_snapshot("op-1")
        finally:
            store.close()
            temporary.cleanup()

    def test_rehydrate_claim_and_effect_query_errors_are_store_integrity_errors(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            connection = store._connection
            assert connection is not None
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TABLE operation_attempts")
            with self.assertRaises(StoreIntegrityError):
                store._rehydrate_claim("op-1")
            with self.assertRaises(StoreIntegrityError):
                store._rehydrate_effect("op-1")
            with self.assertRaises(StoreIntegrityError):
                store._rehydrate_receipt("op-1")
            with self.assertRaises(StoreIntegrityError):
                store._reserve_floor()
            with store._recovery_transaction() as transaction:
                with self.assertRaises(StoreIntegrityError):
                    transaction.claim("op-1")
                with self.assertRaises(StoreIntegrityError):
                    transaction.effect("op-1")
                with self.assertRaises(StoreIntegrityError):
                    transaction.receipt("op-1")
        finally:
            store.close()
            temporary.cleanup()

    def test_constructor_lease_failure_releases_all_resources(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        store.close()
        try:

            class FailingOpenStore(CoordinationStore):
                def _validate_prepared_markers(self) -> None:
                    raise ClockRollbackError("injected constructor failure")

            with self.assertRaises(ClockRollbackError):
                FailingOpenStore(state_root, clock=clock)
            with CoordinationStore(state_root, clock=clock):
                pass
        finally:
            temporary.cleanup()

    def test_unknown_provider_reservation_stops_without_retry(self) -> None:
        class TimeoutProvider(FakeProvider):
            def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
                raise TimeoutError("provider timeout")

        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(ProviderProofError):
                store.reserve_fence(claim, TimeoutProvider())
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("UNKNOWN_EFFECT", operation.status)
            with self.assertRaises(LeaseConflictError):
                store.reclaim(
                    claim,
                    owner="owner-b",
                    provider_id="provider/test",
                    lease_ttl_ns=20,
                    now_ns=120,
                )
        finally:
            store.close()
            temporary.cleanup()

    def test_unknown_reservation_rejects_fallback_before_provider_call(self) -> None:
        class TimeoutProvider(FakeProvider):
            def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
                raise TimeoutError("provider timeout")

        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(ProviderProofError):
                store.reserve_fence(claim, TimeoutProvider())
            fallback = FakeProvider()
            with self.assertRaises(LeaseConflictError):
                store.reserve_fence(claim, fallback)
            self.assertEqual([], fallback.reservations)
        finally:
            store.close()
            temporary.cleanup()

    def test_unknown_provider_effect_stops_after_effect_prepare(self) -> None:
        class ExecuteTimeoutProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def execute(self, effect: ProviderEffect) -> ProviderStatus:
                self.calls += 1
                raise TimeoutError("provider timeout")

        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            provider = ExecuteTimeoutProvider()
            claim = store.reserve_fence(claim, provider)
            with self.assertRaises(ProviderReceiptError):
                store.execute_effect(claim, provider, now_ns=101)
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("UNKNOWN_EFFECT", operation.status)
            with self.assertRaises(LeaseConflictError):
                store.execute_effect(claim, provider, now_ns=102)
            self.assertEqual(1, provider.calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_provider_clock_rollback_still_commits_unknown_effect(self) -> None:
        class RollbackProvider(FakeProvider):
            def execute(self, effect: ProviderEffect) -> ProviderStatus:
                clock.set(99)
                raise TimeoutError("provider timeout")

        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            provider = RollbackProvider()
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            with self.assertRaises(ProviderReceiptError):
                store.execute_effect(claim, provider)
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("UNKNOWN_EFFECT", operation.status)
            self.assertEqual(100, store._last_clock_ns)
        finally:
            store.close()
            temporary.cleanup()

    def test_forged_provider_status_cannot_record_a_receipt(self) -> None:
        class ForgedStatusProvider(FakeProvider):
            def execute(self, effect: ProviderEffect) -> ProviderStatus:
                status = super().execute(effect)
                return replace(status, owner="owner-forged")

        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            provider = ForgedStatusProvider()
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            with self.assertRaises(ProviderReceiptError):
                store.execute_effect(claim, provider, now_ns=101)
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("UNKNOWN_EFFECT", operation.status)
            self.assertEqual(1, len(provider.executions))
        finally:
            store.close()
            temporary.cleanup()

    def test_unknown_consistency_cannot_be_downgraded_to_absent(self) -> None:
        with self.assertRaises(ValueError):
            ProviderStatus(
                operation_id="op-1",
                effect_key="effect/op-1",
                provider_id="provider/test",
                owner="owner-a",
                attempt=1,
                lease_epoch=0,
                fencing_token=1,
                provider_effect_id=None,
                status="ABSENT",
                consistency="UNKNOWN",
            )
        with self.assertRaises(ValueError):
            ProviderStatus(
                operation_id="op-1",
                effect_key="effect/op-1",
                provider_id="provider/test",
                owner="owner-a",
                attempt=1,
                lease_epoch=0,
                fencing_token=1,
                provider_effect_id=None,
                status="COMPLETED",
                consistency="STRONG",
            )

    def test_public_surface_cannot_forge_receipt_or_bypass_provider_calls(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            provider = FakeProvider()
            for name in (
                "activate_fence",
                "begin_effect",
                "record_receipt",
                "mark_unknown_effect",
            ):
                self.assertFalse(callable(getattr(store, name, None)), name)
            self.assertFalse(
                callable(getattr(VerifiedProviderReceipt, "from_status", None))
            )
            self.assertFalse(callable(getattr(ProviderEffect, "_issue", None)))
            with self.assertRaises(TypeError):
                ProviderEffect()
            with self.assertRaises(TypeError):
                VerifiedProviderReceipt()
            with self.assertRaises(ProviderReceiptError):
                store.complete(object.__new__(VerifiedProviderReceipt))
            proof = ProviderFenceProof(
                operation_id="op-1",
                effect_key="effect/op-1",
                provider_id="provider/test",
                owner="owner-a",
                attempt=1,
                lease_epoch=0,
                fencing_token=1,
                proof_version=1,
                proof_ref="proof/1",
            )
            self.assertIsNotNone(proof)
            with self.assertRaises(
                (LeaseConflictError, ProviderReceiptError, TypeError, ValueError)
            ):
                store.complete(
                    proof,  # type: ignore[arg-type]
                    now_ns=101,
                )
            self.assertEqual([], provider.reservations)
            self.assertEqual([], provider.executions)
        finally:
            store.close()
            temporary.cleanup()

    def test_fence_reservation_is_durable_before_provider_call(self) -> None:
        class ObserveReservationProvider(FakeProvider):
            def __init__(self, store: CoordinationStore) -> None:
                super().__init__(store)
                self.observed_status: str | None = None

            def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
                assert self.store is not None
                operation = self.store.operation(effect.operation_id)
                assert operation is not None
                self.observed_status = operation.status
                return super().reserve_fence(effect)

        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            provider = ObserveReservationProvider(store)
            store.reserve_fence(claim, provider)
            self.assertEqual("FENCE_RESERVATION_STARTED", provider.observed_status)
        finally:
            store.close()
            temporary.cleanup()

    def test_reopen_does_not_recover_a_live_provider_call(self) -> None:
        temporary, store = self._store(FakeClock())
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        store.close()
        started = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        class BlockingProvider(FakeProvider):
            def execute(self, effect: ProviderEffect) -> ProviderStatus:
                started.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("provider barrier timed out")
                return super().execute(effect)

        def run_effect() -> None:
            try:
                with CoordinationStore(state_root) as worker_store:
                    claim = worker_store.claim(
                        "op-1",
                        owner="owner-a",
                        provider_id="provider/test",
                        lease_ttl_ns=1_000_000_000,
                    )
                    provider = BlockingProvider()
                    claim = worker_store.reserve_fence(claim, provider)
                    worker_store.execute_effect(claim, provider)
            except (LeaseError, StoreError, OSError, ValueError) as error:
                errors.append(error)

        worker = threading.Thread(target=run_effect)
        worker.start()
        try:
            self.assertTrue(started.wait(timeout=10))
            with CoordinationStore(state_root) as observer:
                operation = observer.operation("op-1")
                assert operation is not None
                self.assertEqual("EFFECT_PREPARED", operation.status)
            with CoordinationStore(state_root, busy_timeout_ms=100) as writer:
                writer.create_intent(
                    "op-other",
                    effect_key="effect/op-other",
                    actor="writer",
                )
            replacement_results: list[str] = []

            def try_replacement() -> None:
                try:
                    with CoordinationStore._exclusive_lifetime_gate_for_root(
                        state_root,
                        busy_timeout_ms=100,
                    ):
                        replacement_results.append("acquired")
                except StoreError:
                    replacement_results.append("blocked")

            replacement = threading.Thread(target=try_replacement)
            replacement.start()
            replacement.join(timeout=5)
            self.assertFalse(replacement.is_alive())
            self.assertEqual(["blocked"], replacement_results)
            release.set()
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            with CoordinationStore(state_root) as final_store:
                operation = final_store.operation("op-1")
            assert operation is not None
            self.assertEqual("RECEIPTED", operation.status)
        finally:
            release.set()
            worker.join(timeout=10)
            store.close()
            temporary.cleanup()

    def test_recovery_snapshot_is_cas_protected_after_heartbeat(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, FakeProvider())
            snapshot = store._recovery_snapshot("op-1")
            store.heartbeat(claim, lease_ttl_ns=20, now_ns=101)
            with (
                self.assertRaises(LeaseConflictError),
                store._recovery_transaction() as transaction,
            ):
                transaction.mark_prepared_unknown(
                    snapshot,
                    actor="owner-a",
                    timestamp=102,
                )
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("CLAIMED", operation.status)
        finally:
            store.close()
            temporary.cleanup()

    def test_recovery_transaction_facade_is_closed_after_context_exit(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        transaction = None
        try:
            with store._recovery_transaction() as active:
                transaction = active
                active.snapshot("op-1")
            assert transaction is not None
            with self.assertRaises(StoreClosedError):
                transaction.snapshot("op-1")
        finally:
            store.close()
            temporary.cleanup()

    def test_recovery_transaction_facade_close_after_store_close_is_stable(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        context = store._recovery_transaction()
        transaction = context.__enter__()
        try:
            snapshot = transaction.snapshot("op-1")
            store.close()
            with self.assertRaises(StoreClosedError):
                transaction.snapshot("op-1")
            with self.assertRaises(StoreClosedError):
                transaction.rebase(
                    snapshot,
                    mode="INTENT",
                    actor="operator",
                    timestamp=snapshot.updated_ns + 1,
                )
            with self.assertRaises(StoreClosedError):
                transaction.claim("op-1")
            with self.assertRaises(StoreClosedError):
                transaction.effect("op-1")
            with self.assertRaises(StoreClosedError):
                transaction.receipt("op-1")
        finally:
            with self.assertRaises(StoreClosedError):
                context.__exit__(None, None, None)
            temporary.cleanup()

    def test_recovery_event_validation_precedes_state_mutation(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            snapshot = store._recovery_snapshot("op-1")
            with store._recovery_transaction() as transaction:
                self.assertFalse(callable(getattr(transaction, "transition", None)))
                with self.assertRaises(ValueError):
                    transaction.append_event(
                        snapshot,
                        kind="recover",
                        reason_code="recover",
                        actor="prompt text",
                        timestamp=101,
                    )
                transaction.append_event(
                    snapshot,
                    kind="recover",
                    reason_code="recover",
                    actor="operator",
                    timestamp=101,
                )
            operation = store.operation("op-1")
            assert operation is not None
            self.assertEqual("INTENT", operation.status)
            self.assertEqual(2, len(store.events("op-1")))
        finally:
            store.close()
            temporary.cleanup()

    def test_reclaim_accepts_canonical_claim_keyword(self) -> None:
        clock = FakeClock()
        temporary, store = self._store(clock)
        try:
            claim = store.claim(
                "op-1",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            replacement = store.reclaim(
                claim=claim,
                owner="owner-b",
                lease_ttl_ns=20,
                now_ns=120,
            )
            self.assertEqual(2, replacement.attempt)
        finally:
            store.close()
            temporary.cleanup()

    def test_provider_rejects_stale_calls_in_both_reservation_orders(self) -> None:
        def effect(owner: str, attempt: int, token: int) -> ProviderEffect:
            proof = ProviderFenceProof(
                operation_id="op-1",
                effect_key="effect/op-1",
                provider_id="provider/test",
                owner=owner,
                attempt=attempt,
                lease_epoch=0,
                fencing_token=token,
                proof_version=1,
                proof_ref=f"proof/{token}",
            )
            return _issue_provider_effect(
                operation_id="op-1",
                effect_key="effect/op-1",
                provider_id="provider/test",
                owner=owner,
                attempt=attempt,
                lease_epoch=0,
                fencing_token=token,
                fence_proof=proof,
            )

        old = effect("owner-a", 1, 1)
        new = effect("owner-b", 2, 2)
        old_first = FakeProvider()
        old_first.reserve_fence(old)
        old_first.reserve_fence(new)
        with self.assertRaises(ProviderProofError):
            old_first.execute(old)

        new_first = FakeProvider()
        new_first.reserve_fence(new)
        with self.assertRaises(ProviderProofError):
            new_first.reserve_fence(old)

        receipt = new_first.execute(new)
        replay = new_first.execute(new)
        self.assertEqual(receipt.provider_effect_id, replay.provider_effect_id)
        self.assertEqual(2, len(new_first.executions))

    def test_two_processes_reclaim_at_expiry_have_one_winner(self) -> None:
        clock = FakeClock(100)
        temporary, store = self._store(clock)
        try:
            store.create_intent(
                "op-race",
                effect_key="effect/op-race",
                provider_id="provider/test",
                actor="main",
                clock_ns=100,
            )
            claim = store.claim(
                "op-race",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            store.reserve_fence(claim, FakeProvider())
            state_root = str(Path(os.path.realpath(temporary.name)) / "state")
            store.close()
            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            results = context.Queue()
            processes = [
                context.Process(
                    target=_reclaim_worker,
                    args=(state_root, owner, barrier, results),
                )
                for owner in ("owner-b", "owner-c")
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("reclaim worker did not exit")
            self.assertEqual(
                ["claimed", "conflict"], sorted(results.get() for _ in processes)
            )
            reopened = CoordinationStore(Path(state_root), clock=clock)
            try:
                operation = reopened.operation("op-race")
                assert operation is not None
                self.assertEqual(2, operation.attempt)
                self.assertEqual("FENCE_PENDING", operation.status)
            finally:
                reopened.close()
        finally:
            store.close()
            temporary.cleanup()

    def test_sigkill_after_effect_prepare_reopens_as_recovery_required(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-lease-kill-")
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        state_root.mkdir(mode=0o700)
        store = CoordinationStore(state_root)
        store.create_intent(
            "op-kill",
            effect_key="effect/op-kill",
            provider_id="provider/test",
            actor="main",
        )
        store.close()
        try:
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_kill_after_effect_prepare,
                args=(str(state_root),),
            )
            process.start()
            process.join(timeout=15)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail("SIGKILL worker did not exit")
            self.assertEqual(-signal.SIGKILL, process.exitcode)
            reopened = CoordinationStore(state_root)
            try:
                operation = reopened.operation("op-kill")
                assert operation is not None
                self.assertEqual("EFFECT_PREPARED", operation.status)
                snapshot = reopened._recovery_snapshot("op-kill")
                with reopened._recovery_transaction() as transaction:
                    transaction.mark_prepared_unknown(
                        snapshot,
                        actor="owner-a",
                        timestamp=snapshot.updated_ns + 1,
                    )
                operation = reopened.operation("op-kill")
                assert operation is not None
                self.assertEqual("UNKNOWN_EFFECT", operation.status)
            finally:
                reopened.close()
        finally:
            temporary.cleanup()

    def test_sigkill_during_fence_reservation_reopens_as_recovery_required(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-lease-fence-kill-")
        state_root = Path(os.path.realpath(temporary.name)) / "state"
        state_root.mkdir(mode=0o700)
        store = CoordinationStore(state_root)
        store.create_intent(
            "op-fence-kill",
            effect_key="effect/op-fence-kill",
            provider_id="provider/test",
            actor="main",
        )
        store.close()
        try:
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_kill_after_fence_reservation_start,
                args=(str(state_root),),
            )
            process.start()
            process.join(timeout=15)
            if process.is_alive():
                process.kill()
                process.join()
                self.fail("SIGKILL fence worker did not exit")
            self.assertEqual(-signal.SIGKILL, process.exitcode)
            reopened = CoordinationStore(state_root)
            try:
                operation = reopened.operation("op-fence-kill")
                assert operation is not None
                self.assertEqual("FENCE_RESERVATION_STARTED", operation.status)
                snapshot = reopened._recovery_snapshot("op-fence-kill")
                with reopened._recovery_transaction() as transaction:
                    transaction.mark_prepared_unknown(
                        snapshot,
                        actor="owner-a",
                        timestamp=snapshot.updated_ns + 1,
                    )
                operation = reopened.operation("op-fence-kill")
                assert operation is not None
                self.assertEqual("UNKNOWN_EFFECT", operation.status)
            finally:
                reopened.close()
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
