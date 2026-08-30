from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

from agent_team.doctor import RecoveryLedgerReader, StateFilesystem
from agent_team.lease import (
    ClockRollbackError,
    LeaseConflictError,
    ProviderCapabilities,
    ProviderEffect,
    ProviderFenceProof,
    ProviderPort,
    ProviderReceiptError,
    ProviderStatus,
    VerifiedProviderReceipt,
)
from agent_team.recovery import (
    FORCE_REASON_CODES,
    RECOVERY_LEDGER_BASENAME,
    RECOVERY_LEDGER_VERSION,
    RecoveryAuthorization,
    RecoveryAuthorizationError,
    RecoveryConflictError,
    RecoveryCoordinator,
    RecoveryLayout,
    RecoveryLedgerError,
    RecoveryLedgerInitialization,
    RecoveryLedgerRecord,
    RecoveryLedgerWriter,
    RecoveryRequiredError,
    _issue_recovery_authorization,
    _issue_recovery_ledger_initialization,
)
from agent_team.store import CoordinationStore, StoreError

MARKER_NAME = "writer.marker"


class FakeClock:
    def __init__(self, now_ns: int = 100) -> None:
        self.now_ns = now_ns

    def __call__(self) -> int:
        return self.now_ns

    def set(self, now_ns: int) -> None:
        self.now_ns = now_ns


class RecoveryProvider:
    capabilities = ProviderCapabilities(
        idempotency=True,
        fencing=True,
        strong_status=True,
    )

    def __init__(self) -> None:
        self.statuses: list[ProviderStatus] = []
        self.status_calls = 0
        self.execute_calls = 0
        self.reserve_calls = 0

    def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof:
        self.reserve_calls += 1
        return ProviderFenceProof(
            operation_id=effect.operation_id,
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
        self.execute_calls += 1
        return self._status(effect, "COMPLETED", f"provider/{effect.effect_key}")

    def status(self, effect: ProviderEffect) -> ProviderStatus:
        self.status_calls += 1
        if not self.statuses:
            return self._status(effect, "UNKNOWN", None)
        return self.statuses.pop(0)

    @staticmethod
    def _status(
        effect: ProviderEffect,
        status: str,
        provider_effect_id: str | None,
    ) -> ProviderStatus:
        proof = effect.fence_proof
        return ProviderStatus(
            operation_id=effect.operation_id,
            effect_key=effect.effect_key,
            provider_id=effect.provider_id,
            owner=effect.owner,
            attempt=effect.attempt,
            lease_epoch=effect.lease_epoch,
            fencing_token=effect.fencing_token,
            provider_effect_id=provider_effect_id,
            status=status,  # type: ignore[arg-type]
            consistency="STRONG",
            proof_version=None if proof is None else proof.proof_version,
            proof_ref=None if proof is None else proof.proof_ref,
        )


class WeakStatusProvider(RecoveryProvider):
    capabilities = ProviderCapabilities(
        idempotency=True,
        fencing=True,
        strong_status=False,
    )


class StatusOnlyProvider:
    capabilities = ProviderCapabilities(
        idempotency=True,
        fencing=True,
        strong_status=True,
    )

    def __init__(self, status: ProviderStatus) -> None:
        self.status_value = status
        self.status_calls = 0

    def status(self, effect: ProviderEffect) -> ProviderStatus:
        del effect
        self.status_calls += 1
        return self.status_value


class ForgedStatus(ProviderStatus):
    pass


class ForgedReceipt(VerifiedProviderReceipt):
    @property
    def is_verified(self) -> bool:
        return True

    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __ne__(self, other: object) -> bool:
        del other
        return False


class EqualityOverride:
    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __hash__(self) -> int:
        return hash(self.value)


class MutableLayoutCoordinator(RecoveryCoordinator):
    def mutate_layout(self) -> None:
        object.__setattr__(self, "_RecoveryCoordinator__layout", object())


class KillRecoveryStore(CoordinationStore):
    def __init__(self, state_root: Path, target: str) -> None:
        self.target = target
        super().__init__(state_root, clock=lambda: 100)

    def _fault(self, point: str) -> None:
        if point == self.target:
            os.kill(os.getpid(), signal.SIGKILL)


class KillRecoveryCoordinator(RecoveryCoordinator):
    def __init__(self, store: CoordinationStore, target: str) -> None:
        self.target = target
        super().__init__(store, marker_name=MARKER_NAME)

    def _fault(self, point: str) -> None:
        if point == self.target:
            os.kill(os.getpid(), signal.SIGKILL)


class FifoSwapWriter(RecoveryLedgerWriter):
    def __init__(self, state_root: Path, swap_point: str) -> None:
        self.swap_point = swap_point
        self.swapped = False
        super().__init__(state_root)

    def _fault(self, point: str) -> None:
        if point != self.swap_point or self.swapped:
            return
        self.swapped = True
        ledger = self.state_root / RECOVERY_LEDGER_BASENAME
        old = self.state_root / "recovery.ledger-old"
        ledger.rename(old)
        os.mkfifo(ledger, mode=0o600)


def _fifo_swap_worker(
    state_root: str, swap_point: str, result_queue: multiprocessing.queues.Queue[str]
) -> None:
    writer = FifoSwapWriter(Path(state_root), swap_point)
    try:
        writer.append(
            RecoveryLedgerRecord(
                version=1,
                sequence=2,
                phase="RESTORE_REPLACED",
                restore_generation=1,
                recovery_epoch=1,
                fencing_token_floor=1,
                backup_digest="sha256:" + "a" * 64,
                actor="operator",
                audit_ref="audit/2",
            )
        )
        result_queue.put("success")
    except RecoveryLedgerError:
        result_queue.put("RecoveryLedgerError")


def _kill_recovery_worker(state_root: str, target: str) -> None:
    with KillRecoveryStore(Path(state_root), target) as store:
        KillRecoveryCoordinator(store, target).recover(
            "op-recovery",
            owner="owner-a",
            provider_id="provider/test",
            effect_key="effect/op-recovery",
            now_ns=120,
        )


def _ledger_initialize_worker(
    state_root: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    record = RecoveryLedgerRecord(
        version=1,
        sequence=1,
        phase="RESTORE_PREPARED",
        restore_generation=1,
        recovery_epoch=1,
        fencing_token_floor=1,
        backup_digest="sha256:" + "a" * 64,
        actor="operator",
        audit_ref="audit/1",
    )
    authority = _issue_recovery_ledger_initialization(
        operator_id="operator",
        audit_ref="audit/1",
        request_digest="sha256:" + "b" * 64,
    )
    try:
        barrier.wait(timeout=10)
        RecoveryLedgerWriter(Path(state_root)).initialize(record, authority)
        result_queue.put("initialized")
    except (OSError, RecoveryRequiredError, StoreError, ValueError) as error:
        result_queue.put(type(error).__name__)


class ForceAuthorizer:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[tuple[str, str, str, str]] = []

    def authorize(
        self,
        *,
        operation_id: str,
        operator_id: str,
        reason_code: str,
        audit_ref: str,
    ) -> RecoveryAuthorization:
        self.calls.append((operation_id, operator_id, reason_code, audit_ref))
        if not self.allow:
            raise RecoveryRequiredError("force recovery authorization was denied")
        return _issue_recovery_authorization(
            operation_id=operation_id,
            operator_id=operator_id,
            reason_code=reason_code,
            audit_ref=audit_ref,
        )


class MismatchingForceAuthorizer(ForceAuthorizer):
    def authorize(
        self,
        *,
        operation_id: str,
        operator_id: str,
        reason_code: str,
        audit_ref: str,
    ) -> RecoveryAuthorization:
        del operator_id
        return _issue_recovery_authorization(
            operation_id=operation_id,
            operator_id="different-operator",
            reason_code=reason_code,
            audit_ref=audit_ref,
        )


class ForgedAuthorization(RecoveryAuthorization):
    @property
    def is_verified(self) -> bool:
        return True

    def __getattribute__(self, name: str) -> object:
        if name == "operator_id":
            return "operator"
        return super().__getattribute__(name)


def _root(temporary: str) -> Path:
    root = Path(os.path.realpath(temporary)) / "state"
    root.mkdir(mode=0o700)
    return root


def _store(
    clock: FakeClock,
) -> tuple[tempfile.TemporaryDirectory[str], CoordinationStore]:
    temporary = tempfile.TemporaryDirectory(prefix="agent-team-recovery-")
    store = CoordinationStore(_root(temporary.name), clock=clock)
    store.create_intent(
        "op-recovery",
        effect_key="effect/op-recovery",
        provider_id="provider/test",
        actor="main",
        clock_ns=clock.now_ns,
    )
    return temporary, store


def _claim_and_receipt(
    store: CoordinationStore,
    provider: RecoveryProvider,
    *,
    operation_id: str = "op-recovery",
    now_ns: int = 100,
) -> VerifiedProviderReceipt:
    claim = store.claim(
        operation_id,
        owner="owner-a",
        provider_id="provider/test",
        lease_ttl_ns=20,
        now_ns=now_ns,
    )
    claim = store.reserve_fence(claim, provider)
    return store.execute_effect(claim, provider, now_ns=now_ns + 1)


def _checkpoint(store: CoordinationStore) -> None:
    connection = store._connection
    assert connection is not None
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _recover_worker(
    state_root: str,
    owner: str,
    barrier: multiprocessing.synchronize.Barrier,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    try:
        with CoordinationStore(Path(state_root), clock=lambda: 100) as store:
            coordinator = RecoveryCoordinator(store, marker_name=MARKER_NAME)
            barrier.wait(timeout=10)
            coordinator.recover(
                "op-race",
                owner=owner,
                provider_id="provider/test",
                effect_key="effect/op-race",
                now_ns=120,
            )
        result_queue.put("recovered")
    except RecoveryConflictError:
        result_queue.put("conflict")
    except (OSError, RecoveryRequiredError, StoreError, ValueError) as error:
        result_queue.put(type(error).__name__)


class RecoveryLedgerWriterTest(unittest.TestCase):
    @staticmethod
    def _record(sequence: int, phase: str, generation: int = 1) -> RecoveryLedgerRecord:
        return RecoveryLedgerRecord(
            version=1,
            sequence=sequence,
            phase=phase,  # type: ignore[arg-type]
            restore_generation=generation,
            recovery_epoch=generation,
            fencing_token_floor=generation,
            backup_digest="sha256:" + "a" * 64,
            actor="operator",
            audit_ref=f"audit/{sequence}",
        )

    @staticmethod
    def _initialization() -> RecoveryLedgerInitialization:
        return _issue_recovery_ledger_initialization(
            operator_id="operator",
            audit_ref="audit/1",
            request_digest="sha256:" + "b" * 64,
        )

    def test_writer_emits_reader_compatible_owner_only_jsonl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(
                root,
                marker_name=MARKER_NAME,
            )
            digest = "sha256:" + hashlib.sha256(b"backup").hexdigest()
            record = RecoveryLedgerRecord(
                version=RECOVERY_LEDGER_VERSION,
                sequence=1,
                phase="RESTORE_PREPARED",
                restore_generation=1,
                recovery_epoch=2,
                fencing_token_floor=4,
                backup_digest=digest,
                actor="operator",
                audit_ref="audit/1",
            )
            written = writer.initialize(record, self._initialization())
            self.assertEqual(record, written)
            self.assertEqual(
                0o600, (root / RECOVERY_LEDGER_BASENAME).stat().st_mode & 0o777
            )
            with StateFilesystem.open_existing(
                root,
                marker_name=MARKER_NAME,
                ledger_name=RECOVERY_LEDGER_BASENAME,
            ) as filesystem:
                latest = RecoveryLedgerReader().read(filesystem)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(record.sequence, latest.sequence)
            self.assertEqual(record.phase, latest.phase)

    def test_writer_rejects_non_monotonic_or_malformed_existing_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            ledger = root / RECOVERY_LEDGER_BASENAME
            ledger.write_bytes(b'{"version":1,"sequence":')
            ledger.chmod(0o600)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            digest = "sha256:" + "a" * 64
            record = RecoveryLedgerRecord(
                version=1,
                sequence=1,
                phase="RESTORE_PREPARED",
                restore_generation=1,
                recovery_epoch=1,
                fencing_token_floor=1,
                backup_digest=digest,
                actor="operator",
                audit_ref="audit/1",
            )
            with self.assertRaises(RecoveryRequiredError):
                writer.append(record)
            self.assertEqual(b'{"version":1,"sequence":', ledger.read_bytes())

    def test_writer_requires_ordered_phases_and_next_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            writer.initialize(
                self._record(1, "RESTORE_PREPARED"), self._initialization()
            )
            for record in (
                self._record(2, "RESTORE_REPLACED"),
                self._record(3, "RESTORE_COMMITTED"),
                self._record(4, "RESTORE_PREPARED", generation=2),
            ):
                writer.append(record)
            latest = writer.read()
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(4, latest.sequence)
            with self.assertRaises(RecoveryRequiredError):
                writer.append(self._record(6, "RESTORE_REPLACED", generation=2))
            with self.assertRaises(RecoveryRequiredError):
                writer.append(self._record(5, "RESTORE_COMMITTED", generation=2))

    def test_append_requires_explicit_initialization_and_missing_history_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            with self.assertRaises(RecoveryRequiredError):
                writer.append(first)
            writer.initialize(first, self._initialization())
            (root / RECOVERY_LEDGER_BASENAME).unlink()
            with self.assertRaises(RecoveryRequiredError):
                writer.append(first)

    def test_ledger_writer_rejects_marker_collision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            with self.assertRaises(ValueError):
                RecoveryLedgerWriter(
                    root,
                    marker_name=RECOVERY_LEDGER_BASENAME,
                )

    def test_ledger_writer_rejects_mutated_noncanonical_basename(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            writer.ledger_name = "alternate.ledger"
            with self.assertRaises(RecoveryRequiredError):
                writer.initialize(
                    self._record(1, "RESTORE_PREPARED"), self._initialization()
                )

    def test_writer_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            target = Path(temporary) / "outside"
            target.write_bytes(b"outside")
            os.symlink(target, root / RECOVERY_LEDGER_BASENAME)
            with self.assertRaises(RecoveryRequiredError):
                RecoveryLedgerWriter(root, marker_name=MARKER_NAME).append(
                    self._record(1, "RESTORE_PREPARED")
                )
            self.assertEqual(b"outside", target.read_bytes())

    def test_fifo_swap_at_each_ledger_open_returns_without_blocking(self) -> None:
        context = multiprocessing.get_context("spawn")
        for swap_point in ("before_ledger_open", "after_ledger_lock"):
            with self.subTest(swap_point=swap_point):
                temporary = tempfile.TemporaryDirectory(
                    prefix="agent-team-ledger-fifo-"
                )
                root = _root(temporary.name)
                writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
                writer.initialize(
                    self._record(1, "RESTORE_PREPARED"), self._initialization()
                )
                result_queue = context.Queue()
                process = context.Process(
                    target=_fifo_swap_worker,
                    args=(str(root), swap_point, result_queue),
                )
                try:
                    process.start()
                    process.join(timeout=5)
                    if process.is_alive():
                        process.kill()
                        process.join()
                        self.fail("FIFO swap writer blocked")
                    self.assertEqual(0, process.exitcode)
                    self.assertEqual("RecoveryLedgerError", result_queue.get(timeout=5))
                finally:
                    if process.is_alive():
                        process.kill()
                        process.join()
                    temporary.cleanup()

    def test_writer_rejects_forged_record_even_when_fields_look_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            record = self._record(1, "RESTORE_PREPARED")
            writer.initialize(record, self._initialization())
            forged = object.__new__(RecoveryLedgerRecord)
            object.__setattr__(forged, "version", record.version)
            object.__setattr__(forged, "sequence", record.sequence)
            object.__setattr__(forged, "phase", record.phase)
            object.__setattr__(forged, "restore_generation", record.restore_generation)
            object.__setattr__(forged, "recovery_epoch", record.recovery_epoch)
            object.__setattr__(
                forged, "fencing_token_floor", record.fencing_token_floor
            )
            object.__setattr__(forged, "backup_digest", record.backup_digest)
            object.__setattr__(forged, "actor", record.actor)
            object.__setattr__(forged, "audit_ref", record.audit_ref)
            object.__setattr__(forged, "actor", "forged actor")
            with self.assertRaises((TypeError, RecoveryRequiredError, ValueError)):
                writer.append(forged)
            self.assertEqual(record, writer.read())

    def test_writer_rejects_virtual_encoder_override(self) -> None:
        class EncoderOverride(RecoveryLedgerRecord):
            def encoded(self) -> bytes:
                return b'{"version":1,"sequence":2}\n'

        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            writer.initialize(first, self._initialization())
            forged = EncoderOverride(
                version=1,
                sequence=2,
                phase="RESTORE_REPLACED",
                restore_generation=1,
                recovery_epoch=1,
                fencing_token_floor=1,
                backup_digest=first.backup_digest,
                actor="operator",
                audit_ref="audit/2",
            )
            with self.assertRaises((TypeError, RecoveryRequiredError)):
                writer.append(forged)
            self.assertEqual(first, writer.read())

    def test_writer_uses_one_root_descriptor_for_first_initialization(self) -> None:
        class RootSwapWriter(RecoveryLedgerWriter):
            swapped = False

            def _fault(self, point: str) -> None:
                if point != "before_final_check" or self.swapped:
                    return
                self.swapped = True
                old_root = self.state_root.with_name("state-old")
                self.state_root.rename(old_root)
                self.state_root.mkdir(mode=0o700)
                (old_root / RECOVERY_LEDGER_BASENAME).rename(
                    self.state_root / RECOVERY_LEDGER_BASENAME
                )

        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RootSwapWriter(root, marker_name=MARKER_NAME)
            with self.assertRaises(RecoveryRequiredError):
                writer.initialize(
                    self._record(1, "RESTORE_PREPARED"), self._initialization()
                )

    def test_writer_rejects_in_place_content_mutation_before_final_check(self) -> None:
        class ContentMutationWriter(RecoveryLedgerWriter):
            mutated = False
            initialized = False

            def _fault(self, point: str) -> None:
                if (
                    point != "before_final_check"
                    or self.mutated
                    or not getattr(self, "initialized", False)
                ):
                    return
                self.mutated = True
                ledger = self.state_root / RECOVERY_LEDGER_BASENAME
                data = ledger.read_bytes().replace(b"operator", b"operat0r", 1)
                ledger.write_bytes(data)

        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = ContentMutationWriter(root, marker_name=MARKER_NAME)
            writer.initialize(
                self._record(1, "RESTORE_PREPARED"), self._initialization()
            )
            writer.initialized = True
            with self.assertRaises(RecoveryRequiredError):
                writer.append(self._record(2, "RESTORE_REPLACED"))

    def test_writer_rejects_in_place_content_mutation_after_lock_before_append(
        self,
    ) -> None:
        class ContentMutationWriter(RecoveryLedgerWriter):
            mutated = False
            initialized = False

            def _fault(self, point: str) -> None:
                if point != "after_ledger_lock" or self.mutated or not self.initialized:
                    return
                self.mutated = True
                ledger = self.state_root / RECOVERY_LEDGER_BASENAME
                data = ledger.read_bytes().replace(b"operator", b"operat0r", 1)
                ledger.write_bytes(data)

        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = ContentMutationWriter(root, marker_name=MARKER_NAME)
            writer.initialize(
                self._record(1, "RESTORE_PREPARED"), self._initialization()
            )
            writer.initialized = True
            with self.assertRaises(RecoveryRequiredError):
                writer.append(self._record(2, "RESTORE_REPLACED"))


class RecoveryLayoutTest(unittest.TestCase):
    def test_layout_is_frozen_and_canonical(self) -> None:
        layout = RecoveryLayout(marker_name=MARKER_NAME)
        self.assertEqual(RECOVERY_LEDGER_BASENAME, layout.ledger_name)
        with self.assertRaises(FrozenInstanceError):
            layout.ledger_name = "alternate.ledger"  # type: ignore[misc]

    def test_coordinator_layout_cannot_be_mutated_or_hide_pending_ledger(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            RecoveryLedgerWriter(store.state_root, marker_name=MARKER_NAME).initialize(
                RecoveryLedgerRecord(
                    version=1,
                    sequence=1,
                    phase="RESTORE_PREPARED",
                    restore_generation=1,
                    recovery_epoch=1,
                    fencing_token_floor=1,
                    backup_digest="sha256:" + "a" * 64,
                    actor="operator",
                    audit_ref="audit/1",
                ),
                _issue_recovery_ledger_initialization(
                    operator_id="operator",
                    audit_ref="audit/1",
                    request_digest="sha256:" + "b" * 64,
                ),
            )
            coordinator = RecoveryCoordinator(store, marker_name=MARKER_NAME)
            with self.assertRaises(AttributeError):
                coordinator.ledger_name = "alternate.ledger"  # type: ignore[misc]
            with self.assertRaises(AttributeError):
                coordinator.marker_name = "alternate.marker"  # type: ignore[misc]
            report = coordinator.startup_preflight("op-recovery")
            self.assertEqual("RESTORE_INCOMPLETE", report.observed_state)
            object.__setattr__(coordinator, "_RecoveryCoordinator__layout", object())
            with self.assertRaises(RecoveryRequiredError):
                coordinator.startup_preflight("op-recovery")
        finally:
            store.close()
            temporary.cleanup()

    def test_concurrent_first_initialization_has_one_ledger_winner(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-ledger-race-")
        root = _root(temporary.name)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(4)
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_ledger_initialize_worker,
                args=(str(root), barrier, result_queue),
            )
            for _ in range(4)
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("ledger initialization worker did not exit")
            outcomes = sorted(result_queue.get(timeout=5) for _ in processes)
            self.assertEqual(1, outcomes.count("initialized"), outcomes)
            self.assertEqual(3, len(outcomes) - outcomes.count("initialized"))
            self.assertEqual(
                1, len((root / RECOVERY_LEDGER_BASENAME).read_text().splitlines())
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join()
            temporary.cleanup()


class RecoveryCoordinatorTest(unittest.TestCase):
    def test_expiry_recovery_is_boundary_checked_and_cas_protected(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            coordinator = RecoveryCoordinator(store, marker_name=MARKER_NAME)
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, RecoveryProvider())
            with self.assertRaises(RecoveryConflictError):
                coordinator.recover(
                    "op-recovery",
                    owner=claim.owner,
                    provider_id=claim.provider_id,
                    effect_key=claim.effect_key,
                    now_ns=119,
                )
            with self.assertRaises(RecoveryConflictError):
                coordinator.recover(
                    "op-recovery",
                    owner="owner-other",
                    provider_id=claim.provider_id,
                    effect_key=claim.effect_key,
                    now_ns=120,
                )
            result = coordinator.recover(
                "op-recovery",
                owner=claim.owner,
                provider_id=claim.provider_id,
                effect_key=claim.effect_key,
                now_ns=120,
            )
            self.assertEqual("CLAIMED", result.from_status)
            self.assertEqual("UNKNOWN_EFFECT", result.status)
            self.assertEqual("recover", store.events("op-recovery")[-1].kind)
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(claim, now_ns=121, lease_ttl_ns=20)
        finally:
            store.close()
            temporary.cleanup()

    def test_plain_fence_pending_expiry_recovery_allocates_a_new_attempt(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            coordinator = RecoveryCoordinator(store, marker_name=MARKER_NAME)
            old_claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            result = coordinator.recover(
                "op-recovery",
                owner=old_claim.owner,
                provider_id=old_claim.provider_id,
                effect_key=old_claim.effect_key,
                now_ns=120,
            )
            self.assertEqual("FENCE_PENDING", result.status)
            self.assertEqual(2, result.snapshot.current_attempt)
            self.assertIsNone(result.snapshot.fence_proof_ref)
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(old_claim, now_ns=121, lease_ttl_ns=20)
            new_claim = store._rehydrate_claim("op-recovery")
            self.assertIsNotNone(new_claim)
            assert new_claim is not None
            self.assertEqual(2, new_claim.attempt)
            self.assertEqual("owner-a", new_claim.owner)
            self.assertEqual("FENCE_PENDING", new_claim.phase)
        finally:
            store.close()
            temporary.cleanup()

    def test_force_requires_verified_typed_authorization_and_preserves_unknown(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            coordinator = RecoveryCoordinator(store, marker_name=MARKER_NAME)
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            authorizer = ForceAuthorizer()
            with self.assertRaises((TypeError, ValueError, RecoveryRequiredError)):
                coordinator.force_recover(
                    "op-recovery",
                    operator_id="operator",
                    reason_code="not-approved",
                    audit_ref="audit/1",
                    authorizer=authorizer,
                    now_ns=101,
                )
            result = coordinator.force_recover(
                "op-recovery",
                operator_id="operator",
                reason_code=FORCE_REASON_CODES[0],
                audit_ref="audit/1",
                authorizer=authorizer,
                now_ns=101,
            )
            self.assertEqual("UNKNOWN_EFFECT", result.status)
            self.assertEqual("force_recover", store.events("op-recovery")[-1].kind)
            self.assertEqual(1, len(authorizer.calls))
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(claim, now_ns=102, lease_ttl_ns=20)
        finally:
            store.close()
            temporary.cleanup()

    def test_force_rejects_mismatched_authorization_without_mutation(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            events_before = store.events("op-recovery")
            with self.assertRaises(RecoveryAuthorizationError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).force_recover(
                    "op-recovery",
                    operator_id="operator",
                    reason_code=FORCE_REASON_CODES[0],
                    audit_ref="audit/1",
                    authorizer=MismatchingForceAuthorizer(),
                    now_ns=101,
                )
            operation = store.operation("op-recovery")
            assert operation is not None
            self.assertEqual("FENCE_PENDING", operation.status)
            self.assertEqual(events_before, store.events("op-recovery"))
        finally:
            store.close()
            temporary.cleanup()

    def test_force_rejects_authorization_subclass_override(self) -> None:
        class SubclassAuthorizer(ForceAuthorizer):
            def authorize(
                self,
                *,
                operation_id: str,
                operator_id: str,
                reason_code: str,
                audit_ref: str,
            ) -> RecoveryAuthorization:
                issued = _issue_recovery_authorization(
                    operation_id=operation_id,
                    operator_id=operator_id,
                    reason_code=reason_code,
                    audit_ref=audit_ref,
                )
                forged = object.__new__(ForgedAuthorization)
                for field_name in (
                    "operation_id",
                    "operator_id",
                    "reason_code",
                    "audit_ref",
                ):
                    object.__setattr__(
                        forged,
                        field_name,
                        getattr(issued, field_name),
                    )
                object.__setattr__(forged, "_provenance", object())
                return forged

        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(RecoveryAuthorizationError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).force_recover(
                    "op-recovery",
                    operator_id="operator",
                    reason_code=FORCE_REASON_CODES[0],
                    audit_ref="audit/subclass",
                    authorizer=SubclassAuthorizer(),
                    now_ns=101,
                )
        finally:
            store.close()
            temporary.cleanup()

    def test_force_reason_requires_exact_builtin_before_authorizer(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            authorizer = ForceAuthorizer()
            with self.assertRaises(ValueError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).force_recover(
                    "op-recovery",
                    operator_id="operator",
                    reason_code=cast(str, EqualityOverride(FORCE_REASON_CODES[0])),
                    audit_ref="audit/equality-reason",
                    authorizer=authorizer,
                    now_ns=101,
                )
            self.assertEqual([], authorizer.calls)
            operation = store.operation("op-recovery")
            assert operation is not None
            self.assertEqual("FENCE_PENDING", operation.status)
        finally:
            store.close()
            temporary.cleanup()

    def test_force_operator_and_audit_require_exact_builtin_before_authorizer(
        self,
    ) -> None:
        for field_name in ("operator_id", "audit_ref"):
            with self.subTest(field_name=field_name):
                clock = FakeClock()
                temporary, store = _store(clock)
                try:
                    store.claim(
                        "op-recovery",
                        owner="owner-a",
                        provider_id="provider/test",
                        lease_ttl_ns=20,
                        now_ns=100,
                    )
                    authorizer = ForceAuthorizer()
                    bad_value = cast(
                        str,
                        EqualityOverride(
                            "operator"
                            if field_name == "operator_id"
                            else "audit/exact-fields"
                        ),
                    )
                    with self.assertRaises(ValueError):
                        if field_name == "operator_id":
                            RecoveryCoordinator(
                                store, marker_name=MARKER_NAME
                            ).force_recover(
                                "op-recovery",
                                operator_id=bad_value,
                                reason_code=FORCE_REASON_CODES[0],
                                audit_ref="audit/exact-fields",
                                authorizer=authorizer,
                                now_ns=101,
                            )
                        else:
                            RecoveryCoordinator(
                                store, marker_name=MARKER_NAME
                            ).force_recover(
                                "op-recovery",
                                operator_id="operator",
                                reason_code=FORCE_REASON_CODES[0],
                                audit_ref=bad_value,
                                authorizer=authorizer,
                                now_ns=101,
                            )
                    self.assertEqual([], authorizer.calls)
                finally:
                    store.close()
                    temporary.cleanup()

    def test_issued_authorization_requires_exact_scalar_fields(self) -> None:
        class EqualityOverrideAuthorizer(ForceAuthorizer):
            field_name: str = "reason_code"

            def authorize(
                self,
                *,
                operation_id: str,
                operator_id: str,
                reason_code: str,
                audit_ref: str,
            ) -> RecoveryAuthorization:
                issued = _issue_recovery_authorization(
                    operation_id=operation_id,
                    operator_id=operator_id,
                    reason_code=reason_code,
                    audit_ref=audit_ref,
                )
                object.__setattr__(
                    issued,
                    self.field_name,
                    EqualityOverride(getattr(issued, self.field_name)),
                )
                return issued

        clock = FakeClock()
        for field_name in ("operator_id", "reason_code", "audit_ref"):
            with self.subTest(field_name=field_name):
                temporary, store = _store(clock)
                try:
                    store.claim(
                        "op-recovery",
                        owner="owner-a",
                        provider_id="provider/test",
                        lease_ttl_ns=20,
                        now_ns=100,
                    )
                    authorizer = EqualityOverrideAuthorizer()
                    authorizer.field_name = field_name
                    with self.assertRaises(RecoveryAuthorizationError):
                        RecoveryCoordinator(
                            store, marker_name=MARKER_NAME
                        ).force_recover(
                            "op-recovery",
                            operator_id="operator",
                            reason_code=FORCE_REASON_CODES[0],
                            audit_ref="audit/equality-issued",
                            authorizer=authorizer,
                            now_ns=101,
                        )
                    operation = store.operation("op-recovery")
                    assert operation is not None
                    self.assertEqual("FENCE_PENDING", operation.status)
                finally:
                    store.close()
                    temporary.cleanup()

    def test_unknown_resolution_only_accepts_strong_current_absent(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            provider = RecoveryProvider()
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            effect = store._begin_effect(claim, now_ns=101)
            store._mark_unknown_effect(effect, now_ns=102)
            _checkpoint(store)
            proof = claim.fence_proof
            assert proof is not None
            provider.statuses.append(
                ProviderStatus(
                    operation_id=claim.operation_id,
                    effect_key=claim.effect_key,
                    provider_id=claim.provider_id,
                    owner=claim.owner,
                    attempt=claim.attempt,
                    lease_epoch=claim.lease_epoch,
                    fencing_token=claim.fencing_token,
                    provider_effect_id=None,
                    status="ABSENT",
                    consistency="STRONG",
                    proof_version=proof.proof_version,
                    proof_ref=proof.proof_ref,
                )
            )
            result = RecoveryCoordinator(
                store, marker_name=MARKER_NAME
            ).resolve_unknown(
                "op-recovery", provider=provider, actor="operator", now_ns=102
            )
            self.assertEqual("INTENT", result.status)
            self.assertEqual(0, result.snapshot.current_attempt)
            next_claim = store.claim(
                "op-recovery",
                owner="owner-b",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=103,
            )
            self.assertEqual(2, next_claim.attempt)
            with self.assertRaises(LeaseConflictError):
                store.heartbeat(claim, lease_ttl_ns=20, now_ns=104)
            self.assertEqual(1, provider.status_calls)
            self.assertEqual(0, provider.execute_calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_unknown_resolution_checks_global_epoch_before_provider_query(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            provider = RecoveryProvider()
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            effect = store._begin_effect(claim, now_ns=101)
            store._mark_unknown_effect(effect, now_ns=102)
            _checkpoint(store)
            with store._lease_write_transaction() as connection:
                connection.execute(
                    "UPDATE store_meta SET value = 1 WHERE key = 'recovery_epoch'"
                )
            _checkpoint(store)
            with self.assertRaises(RecoveryRequiredError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).resolve_unknown(
                    "op-recovery",
                    provider=provider,
                    actor="operator",
                    now_ns=103,
                )
            self.assertEqual(0, provider.status_calls)
            self.assertEqual(0, provider.execute_calls)
            operation = store.operation("op-recovery")
            assert operation is not None
            self.assertEqual("UNKNOWN_EFFECT", operation.status)
        finally:
            store.close()
            temporary.cleanup()

    def test_unknown_resolution_completed_rebinds_without_execute(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            provider = RecoveryProvider()
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            effect = store._begin_effect(claim, now_ns=101)
            store._mark_unknown_effect(effect, now_ns=102)
            _checkpoint(store)
            proof = claim.fence_proof
            assert proof is not None
            provider.statuses.append(
                ProviderStatus(
                    operation_id=claim.operation_id,
                    effect_key=claim.effect_key,
                    provider_id=claim.provider_id,
                    owner=claim.owner,
                    attempt=claim.attempt,
                    lease_epoch=claim.lease_epoch,
                    fencing_token=claim.fencing_token,
                    provider_effect_id="provider/effect-op-recovery",
                    status="COMPLETED",
                    consistency="STRONG",
                    proof_version=proof.proof_version,
                    proof_ref=proof.proof_ref,
                )
            )
            result = RecoveryCoordinator(
                store, marker_name=MARKER_NAME
            ).resolve_unknown(
                "op-recovery", provider=provider, actor="operator", now_ns=102
            )
            self.assertEqual("RECEIPTED", result.status)
            self.assertIsNotNone(result.receipt)
            assert result.receipt is not None
            self.assertEqual(0, provider.execute_calls)
            self.assertEqual(
                "COMPLETED", store.complete(result.receipt, now_ns=103).status
            )
        finally:
            store.close()
            temporary.cleanup()

    def test_unknown_resolution_rejects_weak_or_mismatched_status_without_execute(
        self,
    ) -> None:
        for variant in ("weak", "mismatch"):
            with self.subTest(variant=variant):
                clock = FakeClock()
                temporary, store = _store(clock)
                try:
                    setup_provider = RecoveryProvider()
                    provider = (
                        WeakStatusProvider()
                        if variant == "weak"
                        else RecoveryProvider()
                    )
                    claim = store.claim(
                        "op-recovery",
                        owner="owner-a",
                        provider_id="provider/test",
                        lease_ttl_ns=20,
                        now_ns=100,
                    )
                    claim = store.reserve_fence(claim, setup_provider)
                    effect = store._begin_effect(claim, now_ns=101)
                    store._mark_unknown_effect(effect, now_ns=102)
                    _checkpoint(store)
                    proof = claim.fence_proof
                    assert proof is not None
                    provider.statuses.append(
                        ProviderStatus(
                            operation_id=claim.operation_id,
                            effect_key=claim.effect_key,
                            provider_id=claim.provider_id,
                            owner=claim.owner,
                            attempt=claim.attempt,
                            lease_epoch=claim.lease_epoch,
                            fencing_token=(
                                claim.fencing_token + 1
                                if variant == "mismatch"
                                else claim.fencing_token
                            ),
                            provider_effect_id=None,
                            status="ABSENT",
                            consistency="STRONG",
                            proof_version=proof.proof_version,
                            proof_ref=proof.proof_ref,
                        )
                    )
                    with self.assertRaises(RecoveryRequiredError):
                        RecoveryCoordinator(
                            store, marker_name=MARKER_NAME
                        ).resolve_unknown(
                            "op-recovery",
                            provider=provider,
                            actor="operator",
                            now_ns=103,
                        )
                    operation = store.operation("op-recovery")
                    assert operation is not None
                    self.assertEqual("UNKNOWN_EFFECT", operation.status)
                    self.assertEqual(0, provider.execute_calls)
                    self.assertEqual(
                        0 if variant == "weak" else 1, provider.status_calls
                    )
                finally:
                    store.close()
                    temporary.cleanup()

    def test_status_only_provider_and_status_subclass_are_rejected(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            setup_provider = RecoveryProvider()
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, setup_provider)
            effect = store._begin_effect(claim, now_ns=101)
            store._mark_unknown_effect(effect, now_ns=102)
            _checkpoint(store)
            proof = claim.fence_proof
            assert proof is not None
            status = ProviderStatus(
                operation_id=claim.operation_id,
                effect_key=claim.effect_key,
                provider_id=claim.provider_id,
                owner=claim.owner,
                attempt=claim.attempt,
                lease_epoch=claim.lease_epoch,
                fencing_token=claim.fencing_token,
                provider_effect_id=None,
                status="ABSENT",
                consistency="STRONG",
                proof_version=proof.proof_version,
                proof_ref=proof.proof_ref,
            )
            status_only = StatusOnlyProvider(status)
            with self.assertRaises(RecoveryRequiredError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).resolve_unknown(
                    "op-recovery",
                    provider=cast(ProviderPort, status_only),
                    actor="operator",
                    now_ns=103,
                )
            self.assertEqual(0, status_only.status_calls)

            forged = object.__new__(ForgedStatus)
            for field_name in (
                "operation_id",
                "effect_key",
                "provider_id",
                "owner",
                "attempt",
                "lease_epoch",
                "fencing_token",
                "provider_effect_id",
                "status",
                "consistency",
                "proof_version",
                "proof_ref",
            ):
                object.__setattr__(forged, field_name, getattr(status, field_name))
            object.__setattr__(forged, "fencing_token", claim.fencing_token + 1)
            forged_provider = RecoveryProvider()
            forged_provider.statuses.append(forged)
            with self.assertRaises(RecoveryRequiredError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).resolve_unknown(
                    "op-recovery",
                    provider=forged_provider,
                    actor="operator",
                    now_ns=103,
                )
            self.assertEqual(1, forged_provider.status_calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_pending_wal_blocks_status_query_before_provider_call(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            provider = RecoveryProvider()
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store.reserve_fence(claim, provider)
            effect = store._begin_effect(claim, now_ns=101)
            store._mark_unknown_effect(effect, now_ns=102)
            with self.assertRaises(RecoveryRequiredError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).resolve_unknown(
                    "op-recovery", provider=provider, actor="operator", now_ns=103
                )
            self.assertEqual(0, provider.status_calls)
            self.assertEqual(0, provider.execute_calls)
        finally:
            store.close()
            temporary.cleanup()

    def test_rebind_receipt_advances_store_issued_floor_and_keeps_receipt(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            provider = RecoveryProvider()
            receipt = _claim_and_receipt(store, provider)
            execute_calls_before = provider.execute_calls
            old_effect_id = receipt.provider_effect_id
            result = RecoveryCoordinator(store, marker_name=MARKER_NAME).rebind_receipt(
                "op-recovery", receipt=receipt, actor="operator", now_ns=103
            )
            self.assertEqual("RECEIPTED", result.status)
            self.assertIsNotNone(result.receipt)
            assert result.receipt is not None
            self.assertEqual(old_effect_id, result.receipt.provider_effect_id)
            self.assertGreater(result.receipt.fencing_token, receipt.fencing_token)
            self.assertEqual(execute_calls_before, provider.execute_calls)
            self.assertEqual(
                "COMPLETED", store.complete(result.receipt, now_ns=104).status
            )
        finally:
            store.close()
            temporary.cleanup()

    def test_rebind_rejects_forged_receipt_subclass_without_equality_or_property_trust(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            receipt = _claim_and_receipt(store, RecoveryProvider())
            forged = object.__new__(ForgedReceipt)
            for field_name in (
                "operation_id",
                "effect_key",
                "provider_id",
                "owner",
                "attempt",
                "lease_epoch",
                "fencing_token",
                "provider_effect_id",
                "provider_status",
                "proof_version",
                "proof_ref",
            ):
                object.__setattr__(forged, field_name, getattr(receipt, field_name))
            object.__setattr__(forged, "owner", "wrong-owner")
            with self.assertRaises(ProviderReceiptError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).rebind_receipt(
                    "op-recovery", receipt=forged, actor="operator", now_ns=103
                )
            self.assertEqual(receipt, store._rehydrate_receipt("op-recovery"))
        finally:
            store.close()
            temporary.cleanup()

    def test_rebind_receipt_requires_exact_builtin_provider_status(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            receipt = _claim_and_receipt(store, RecoveryProvider())
            forged = object.__new__(VerifiedProviderReceipt)
            for field_name in (
                "operation_id",
                "effect_key",
                "provider_id",
                "owner",
                "attempt",
                "lease_epoch",
                "fencing_token",
                "provider_effect_id",
                "provider_status",
                "proof_version",
                "proof_ref",
            ):
                object.__setattr__(forged, field_name, getattr(receipt, field_name))
            object.__setattr__(
                forged,
                "provider_status",
                EqualityOverride("COMPLETED"),
            )
            object.__setattr__(
                forged,
                "_provenance",
                object.__getattribute__(receipt, "_provenance"),
            )
            events_before = store.events("op-recovery")
            with self.assertRaises(ProviderReceiptError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).rebind_receipt(
                    "op-recovery",
                    receipt=forged,
                    actor="operator",
                    now_ns=103,
                )
            operation = store.operation("op-recovery")
            assert operation is not None
            self.assertEqual("RECEIPTED", operation.status)
            self.assertEqual(events_before, store.events("op-recovery"))
        finally:
            store.close()
            temporary.cleanup()

    def test_force_keeps_completed_terminal_state_and_cleaned_is_immutable(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            provider = RecoveryProvider()
            receipt = _claim_and_receipt(store, provider)
            store.complete(receipt, now_ns=102)
            force = RecoveryCoordinator(store, marker_name=MARKER_NAME).force_recover(
                "op-recovery",
                operator_id="operator",
                reason_code=FORCE_REASON_CODES[0],
                audit_ref="audit/completed",
                authorizer=ForceAuthorizer(),
                now_ns=103,
            )
            self.assertEqual("COMPLETED", force.status)
            self.assertIsNotNone(force.receipt)
            assert force.receipt is not None
            self.assertEqual(
                receipt.provider_effect_id, force.receipt.provider_effect_id
            )
            self.assertEqual(1, provider.execute_calls)

            store.create_intent(
                "op-cleaned",
                effect_key="effect/op-cleaned",
                provider_id="provider/test",
                actor="main",
                clock_ns=103,
            )
            store.claim(
                "op-cleaned",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=103,
            )
            with store._lease_write_transaction() as connection:
                connection.execute(
                    "UPDATE operations SET status = 'CLEANED' "
                    "WHERE operation_id = 'op-cleaned'"
                )
            cleaned_before = store.events("op-cleaned")
            with self.assertRaises(RecoveryConflictError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).force_recover(
                    "op-cleaned",
                    operator_id="operator",
                    reason_code=FORCE_REASON_CODES[0],
                    audit_ref="audit/cleaned",
                    authorizer=ForceAuthorizer(),
                    now_ns=104,
                )
            self.assertEqual(cleaned_before, store.events("op-cleaned"))
        finally:
            store.close()
            temporary.cleanup()

    def test_expiry_clock_rollback_is_rejected_without_mutation(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            claim = store.claim(
                "op-recovery",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            with self.assertRaises(ClockRollbackError):
                RecoveryCoordinator(store, marker_name=MARKER_NAME).recover(
                    "op-recovery",
                    owner=claim.owner,
                    provider_id=claim.provider_id,
                    effect_key=claim.effect_key,
                    now_ns=90,
                )
            operation = store.operation("op-recovery")
            assert operation is not None
            self.assertEqual("FENCE_PENDING", operation.status)
        finally:
            store.close()
            temporary.cleanup()

    def test_concurrent_expiry_recovery_has_one_cas_winner(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-recovery-race-")
        root = _root(temporary.name)
        with CoordinationStore(root, clock=lambda: 100) as store:
            store.create_intent(
                "op-race",
                effect_key="effect/op-race",
                provider_id="provider/test",
                actor="main",
                clock_ns=100,
            )
            store.claim(
                "op-race",
                owner="owner-a",
                provider_id="provider/test",
                lease_ttl_ns=20,
                now_ns=100,
            )
            claim = store._rehydrate_claim("op-race")
            assert claim is not None
            store.reserve_fence(claim, RecoveryProvider())
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        result_queue = context.Queue()
        processes = [
            context.Process(
                target=_recover_worker,
                args=(str(root), "owner-a", barrier, result_queue),
            )
            for _ in ("caller-b", "caller-c")
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
                    self.fail("recovery worker did not exit")
            self.assertEqual(
                ["conflict", "recovered"],
                sorted(result_queue.get(timeout=5) for _ in processes),
            )
            with CoordinationStore(root) as store:
                operation = store.operation("op-race")
                assert operation is not None
                self.assertEqual("UNKNOWN_EFFECT", operation.status)
                self.assertEqual(5, len(store.events("op-race")))
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join()
            temporary.cleanup()

    def test_sigkill_recovery_barriers_reopen_without_false_success(self) -> None:
        targets = (
            "before_recovery_transition",
            "after_recovery_row",
            "before_recovery_event",
            "after_recovery_event",
            "before_commit",
            "after_commit",
            "before_result",
            "after_result",
        )
        context = multiprocessing.get_context("spawn")
        for target in targets:
            with self.subTest(target=target):
                temporary = tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-kill-"
                )
                root = _root(temporary.name)
                try:
                    with CoordinationStore(root, clock=lambda: 100) as store:
                        store.create_intent(
                            "op-recovery",
                            effect_key="effect/op-recovery",
                            provider_id="provider/test",
                            actor="main",
                            clock_ns=100,
                        )
                        store.claim(
                            "op-recovery",
                            owner="owner-a",
                            provider_id="provider/test",
                            lease_ttl_ns=20,
                            now_ns=100,
                        )
                        claim = store._rehydrate_claim("op-recovery")
                        assert claim is not None
                        store.reserve_fence(claim, RecoveryProvider())
                    process = context.Process(
                        target=_kill_recovery_worker,
                        args=(str(root), target),
                    )
                    process.start()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join()
                        self.fail("recovery SIGKILL worker did not exit")
                    self.assertEqual(-signal.SIGKILL, process.exitcode)
                    with CoordinationStore(root) as store:
                        operation = store.operation("op-recovery")
                        assert operation is not None
                        events = store.events("op-recovery")
                    committed = target in {
                        "after_commit",
                        "before_result",
                        "after_result",
                    }
                    self.assertEqual(
                        "UNKNOWN_EFFECT" if committed else "CLAIMED",
                        operation.status,
                    )
                    self.assertEqual(5 if committed else 4, len(events))
                finally:
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
