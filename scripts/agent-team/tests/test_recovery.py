from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import signal
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import FrozenInstanceError, replace
from itertools import product
from pathlib import Path
from typing import TypeVar, cast
from unittest import mock

from agent_team import doctor as doctor_module
from agent_team import recovery as recovery_module
from agent_team import store as store_module
from agent_team.doctor import LedgerReadError, RecoveryLedgerReader, StateFilesystem
from agent_team.lease import (
    ClockRollbackError,
    LeaseConflictError,
    ProviderCapabilities,
    ProviderEffect,
    ProviderFenceProof,
    ProviderPort,
    ProviderReceiptError,
    ProviderStatus,
    RecoveryFloor,
    VerifiedProviderReceipt,
)
from agent_team.lease import RestoreIdentity as LeaseRestoreIdentity
from agent_team.recovery import (
    FORCE_REASON_CODES,
    RECOVERY_LEDGER_BASENAME,
    RECOVERY_LEDGER_VERSION,
    RECOVERY_TOMBSTONES_BASENAME,
    RecoveryAuthorization,
    RecoveryAuthorizationError,
    RecoveryConflictError,
    RecoveryCoordinator,
    RecoveryDurabilityError,
    RecoveryLayout,
    RecoveryLedgerError,
    RecoveryLedgerInitialization,
    RecoveryLedgerRecord,
    RecoveryLedgerWriter,
    RecoveryRequiredError,
    RecoveryTombstoneLog,
    RecoveryTombstoneRecord,
    RestoreHandle,
    RestoreIdentity,
    RestoreLedger,
    RestoreTombstoneOrphan,
    TombstonePhase,
    _encode_record,
    _encode_tombstone,
    _issue_recovery_authorization,
    _issue_recovery_ledger_initialization,
    _issue_restore_handle,
    _normal_open_preflight,
)
from agent_team.store import CoordinationStore, StoreError
from agent_team.wal import QuiescenceOwner, QuiescenceSession, WalSidecarController

MARKER_NAME = "writer.marker"
_ResourceT = TypeVar("_ResourceT")


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


class BorrowedRootOwner:
    """Test-only owner shape for the package-private borrowed-root seam."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root
        flags = os.O_RDONLY | os.O_CLOEXEC
        directory = getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if directory == 0 or nofollow == 0:
            raise unittest.SkipTest("secure directory flags are unavailable")
        self._root_fd = os.open(
            state_root,
            flags | directory | nofollow,
        )
        metadata = os.fstat(self._root_fd)
        self._identity = (metadata.st_dev, metadata.st_ino)
        self.closed = False
        self.borrow_count = 0
        self.assert_count = 0

    @contextmanager
    def _borrow_root(self, state_root: Path) -> Iterator[BorrowedRootOwner]:
        if self.closed or state_root != self.state_root:
            raise RecoveryRequiredError("borrowed root is unavailable")
        self.borrow_count += 1
        self.assert_identity()
        try:
            yield self
        finally:
            self.assert_identity()

    def assert_identity(self) -> None:
        if self.closed:
            raise RecoveryRequiredError("borrowed root owner is closed")
        self.assert_count += 1
        fd_metadata = os.fstat(self._root_fd)
        path_metadata = os.stat(self.state_root, follow_symlinks=False)
        if (fd_metadata.st_dev, fd_metadata.st_ino) != self._identity or (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != self._identity:
            raise RecoveryRequiredError("borrowed root identity changed")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        os.close(self._root_fd)


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
        request_digest="sha256:" + "a" * 64,
    )
    try:
        barrier.wait(timeout=10)
        RecoveryLedgerWriter(Path(state_root)).initialize(record, authority)
        result_queue.put("initialized")
    except (OSError, RecoveryRequiredError, StoreError, ValueError) as error:
        result_queue.put(type(error).__name__)


def _paused_fresh_store_worker(
    state_root: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    class PausedFreshStore(CoordinationStore):
        def _open_database_file(self, *, create: bool) -> int:
            if create:
                ready.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("fresh store pause timed out")
            return super()._open_database_file(create=create)

    try:
        with PausedFreshStore(Path(state_root), busy_timeout_ms=100):
            result_queue.put("opened")
    except (
        OSError,
        RecoveryRequiredError,
        StoreError,
        ValueError,
        RuntimeError,
    ) as error:
        result_queue.put(type(error).__name__)


def _paused_existing_store_worker(
    state_root: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    class PausedExistingStore(CoordinationStore):
        preflight_calls = 0

        def _run_normal_open_preflight(self) -> object:
            result = super()._run_normal_open_preflight()
            type(self).preflight_calls += 1
            if type(self).preflight_calls == 2:
                ready.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("existing store pause timed out")
            return result

    try:
        with PausedExistingStore(Path(state_root), busy_timeout_ms=100):
            result_queue.put("opened")
    except (
        OSError,
        RecoveryRequiredError,
        StoreError,
        ValueError,
        RuntimeError,
    ) as error:
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


def _write_recovery_pair(
    root: Path,
    ledger_records: tuple[RecoveryLedgerRecord, ...],
    tombstone_records: tuple[RecoveryTombstoneRecord, ...],
) -> None:
    ledger_path = root / RECOVERY_LEDGER_BASENAME
    tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
    ledger_path.write_bytes(
        b"".join(_encode_record(record) for record in ledger_records)
    )
    tombstone_path.write_bytes(
        b"".join(_encode_tombstone(record) for record in tombstone_records)
    )
    ledger_path.chmod(0o600)
    tombstone_path.chmod(0o600)


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
            audit_ref=f"audit/{generation}",
        )

    @staticmethod
    def _initialization(
        request_digest: str = "sha256:" + "a" * 64,
    ) -> RecoveryLedgerInitialization:
        return _issue_recovery_ledger_initialization(
            operator_id="operator",
            audit_ref="audit/1",
            request_digest=request_digest,
        )

    def test_writer_emits_reader_compatible_owner_only_jsonl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
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
            written = writer.initialize(
                record,
                self._initialization(record.backup_digest),
            )
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

    def test_initialization_authority_binds_request_digest_to_record(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-ledger-") as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            record = self._record(1, "RESTORE_PREPARED")
            mismatched = self._initialization("sha256:" + "b" * 64)
            with self.assertRaises(RecoveryRequiredError):
                writer.initialize(record, mismatched)
            self.assertFalse((root / RECOVERY_LEDGER_BASENAME).exists())

    def test_owned_ledger_io_uses_borrowed_root_without_reacquiring_filesystem(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-owned-ledger-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            second = self._record(2, "RESTORE_REPLACED")
            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    mock.patch(
                        "agent_team.recovery.StateFilesystem.open_existing",
                        side_effect=AssertionError(
                            "owner path reacquired StateFilesystem"
                        ),
                    ),
                ):
                    writer.initialize_owned(
                        first,
                        self._initialization(first.backup_digest),
                        owner=owner,
                    )
                    writer.append_owned(second, owner=owner)
                    self.assertEqual(second, writer.read_owned(owner=owner))
                self.assertGreaterEqual(owner.borrow_count, 3)
            finally:
                owner.close()

    def test_owned_ledger_rejects_closed_or_forged_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-owned-ledger-"
        ) as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            record = self._record(1, "RESTORE_PREPARED")
            owner = BorrowedRootOwner(root)
            owner.close()
            with (
                mock.patch("agent_team.recovery.QuiescenceOwner", BorrowedRootOwner),
                self.assertRaises(RecoveryRequiredError),
            ):
                writer.initialize_owned(
                    record,
                    self._initialization(record.backup_digest),
                    owner=owner,
                )
            with self.assertRaises(RecoveryRequiredError):
                writer.append_owned(record, owner=object())
            with self.assertRaises(RecoveryRequiredError):
                writer.append_owned(record, owner=object.__new__(QuiescenceOwner))

    def test_owned_append_close_failure_is_typed_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-owned-ledger-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            second = self._record(2, "RESTORE_REPLACED")
            real_open = os.open
            real_close = os.close
            opened_write_fds: list[int] = []

            def capture_open(*args: object, **kwargs: object) -> int:
                fd = real_open(*args, **kwargs)  # type: ignore[arg-type]
                flags = args[1]
                if type(flags) is int and flags & os.O_WRONLY:
                    opened_write_fds.append(fd)
                return fd

            def fail_write_fd_close(fd: int) -> None:
                if fd in opened_write_fds:
                    raise OSError("simulated close uncertainty")
                real_close(fd)

            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    writer.initialize_owned(
                        first,
                        self._initialization(first.backup_digest),
                        owner=owner,
                    )
                    with (
                        mock.patch(
                            "agent_team.recovery.os.open", side_effect=capture_open
                        ),
                        mock.patch(
                            "agent_team.recovery.os.close",
                            side_effect=fail_write_fd_close,
                        ),
                        self.assertRaises(RecoveryLedgerError),
                    ):
                        writer.append_owned(second, owner=owner)
                    self.assertEqual(second, writer.read_owned(owner=owner))
            finally:
                for fd in opened_write_fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass
                owner.close()

    def test_owned_append_close_failure_handoffs_fd_to_quiescence_resources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-owned-ledger-close-retry-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=100)
            session = controller.hold_quiescence(
                allowed_root_names=(),
            )
            owner = session.issue_owner()
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            record = self._record(1, "RESTORE_PREPARED")
            authority = self._initialization(record.backup_digest)
            _, _, resources = owner._provenance()
            real_open = os.open
            real_close = os.close
            opened_write_fds: list[int] = []

            def capture_open(*args: object, **kwargs: object) -> int:
                fd = real_open(*args, **kwargs)  # type: ignore[arg-type]
                flags = args[1]
                if type(flags) is int and flags & os.O_WRONLY:
                    opened_write_fds.append(fd)
                return fd

            def fail_write_fd_close(fd: int) -> None:
                if fd in opened_write_fds:
                    raise OSError("simulated close uncertainty")
                real_close(fd)

            try:
                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=fail_write_fd_close,
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    writer.initialize_owned(record, authority, owner=owner)
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                raised.exception.retry_cleanup()
                self.assertEqual(0, len(resources._orphan_fds))
            finally:
                try:
                    session.close()
                finally:
                    for fd in opened_write_fds:
                        try:
                            real_close(fd)
                        except OSError:
                            pass

    def test_ledger_append_rejects_same_generation_actor_or_audit_changes(
        self,
    ) -> None:
        for field_name, changed_value in (
            ("actor", "different-actor"),
            ("audit_ref", "audit/different"),
        ):
            with (
                self.subTest(field_name=field_name),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-ledger-provenance-"
                ) as temporary,
            ):
                root = _root(temporary)
                writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
                first = self._record(1, "RESTORE_PREPARED")
                second = replace(
                    self._record(2, "RESTORE_REPLACED"),
                    actor=changed_value if field_name == "actor" else first.actor,
                    audit_ref=(
                        changed_value if field_name == "audit_ref" else first.audit_ref
                    ),
                )
                writer.initialize(first, self._initialization(first.backup_digest))
                with self.assertRaises(RecoveryLedgerError):
                    writer.append(second)

    def test_unowned_append_requires_exclusive_quiescence_when_store_is_live(
        self,
    ) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            writer = RecoveryLedgerWriter(store.state_root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            with self.assertRaises(RecoveryLedgerError):
                writer.initialize(first, self._initialization(first.backup_digest))
            store.close()
            self.assertEqual(
                first,
                writer.initialize(first, self._initialization(first.backup_digest)),
            )
        finally:
            store.close()
            temporary.cleanup()

    def test_unowned_append_retains_session_when_close_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-unowned-session-close-retry-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            record = self._record(1, "RESTORE_PREPARED")
            original_close = QuiescenceSession.close
            failed = False

            def fail_once(session: QuiescenceSession) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("simulated quiescence close uncertainty")
                original_close(session)

            with (
                mock.patch.object(QuiescenceSession, "close", new=fail_once),
                self.assertRaises(RecoveryLedgerError) as raised,
            ):
                writer.initialize(record, self._initialization(record.backup_digest))
            self.assertEqual(1, len(writer._orphan_sessions))
            self.assertIsNotNone(raised.exception.cleanup_owner)
            raised.exception.retry_cleanup()
            self.assertIsNone(raised.exception.cleanup_owner)
            writer.close()
            self.assertEqual([], writer._orphan_sessions)
            self.assertEqual(record, writer.read())

    def test_unowned_first_log_cannot_race_fresh_store_database_open(self) -> None:
        context = multiprocessing.get_context("spawn")
        for log_kind in ("ledger", "tombstone"):
            with (
                self.subTest(log_kind=log_kind),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-fresh-store-race-"
                ) as temporary,
            ):
                root = _root(temporary)
                ready = context.Event()
                release = context.Event()
                result_queue = context.Queue()
                process = context.Process(
                    target=_paused_fresh_store_worker,
                    args=(str(root), ready, release, result_queue),
                )
                if log_kind == "ledger":
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(
                            1,
                            "RESTORE_PREPARED",
                        )
                    )
                    writer: RecoveryLedgerWriter | RecoveryTombstoneLog = (
                        RecoveryLedgerWriter(root, busy_timeout_ms=50)
                    )
                    authority = self._initialization(record.backup_digest)
                else:
                    record = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                    )
                    writer = RecoveryTombstoneLog(root, busy_timeout_ms=50)
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                path = root / (
                    RECOVERY_LEDGER_BASENAME
                    if log_kind == "ledger"
                    else RECOVERY_TOMBSTONES_BASENAME
                )
                process.start()
                try:
                    self.assertTrue(ready.wait(timeout=10))
                    with self.assertRaises(RecoveryLedgerError):
                        if log_kind == "ledger":
                            assert isinstance(record, RecoveryLedgerRecord)
                            assert isinstance(writer, RecoveryLedgerWriter)
                            writer.initialize(record, authority)
                        else:
                            assert isinstance(record, RecoveryTombstoneRecord)
                            assert isinstance(writer, RecoveryTombstoneLog)
                            writer.initialize(record, authority)
                    self.assertFalse(path.exists())
                finally:
                    release.set()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join()
                self.assertEqual(0, process.exitcode)
                self.assertEqual("opened", result_queue.get(timeout=5))

    def test_unowned_append_cannot_race_second_store_preflight(self) -> None:
        context = multiprocessing.get_context("spawn")
        for log_kind in ("ledger", "tombstone"):
            with (
                self.subTest(log_kind=log_kind),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-second-preflight-race-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                ready = context.Event()
                release = context.Event()
                result_queue = context.Queue()
                process = context.Process(
                    target=_paused_existing_store_worker,
                    args=(str(root), ready, release, result_queue),
                )
                if log_kind == "ledger":
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(
                            1,
                            "RESTORE_PREPARED",
                        )
                    )
                    writer: RecoveryLedgerWriter | RecoveryTombstoneLog = (
                        RecoveryLedgerWriter(root, busy_timeout_ms=50)
                    )
                    authority = self._initialization(record.backup_digest)
                else:
                    record = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                    )
                    writer = RecoveryTombstoneLog(root, busy_timeout_ms=50)
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                path = root / (
                    RECOVERY_LEDGER_BASENAME
                    if log_kind == "ledger"
                    else RECOVERY_TOMBSTONES_BASENAME
                )
                process.start()
                try:
                    self.assertTrue(ready.wait(timeout=10))
                    with self.assertRaises(RecoveryLedgerError):
                        if log_kind == "ledger":
                            assert isinstance(record, RecoveryLedgerRecord)
                            assert isinstance(writer, RecoveryLedgerWriter)
                            writer.initialize(record, authority)
                        else:
                            assert isinstance(record, RecoveryTombstoneRecord)
                            assert isinstance(writer, RecoveryTombstoneLog)
                            writer.initialize(record, authority)
                    self.assertFalse(path.exists())
                finally:
                    release.set()
                    process.join(timeout=15)
                    if process.is_alive():
                        process.kill()
                        process.join()
                self.assertEqual(0, process.exitcode)
                self.assertEqual("opened", result_queue.get(timeout=5))

    def test_unowned_read_remains_shared_with_live_store(self) -> None:
        temporary, store = _store(FakeClock())
        try:
            writer = RecoveryLedgerWriter(store.state_root, marker_name=MARKER_NAME)
            self.assertIsNone(writer.read())
            record = self._record(1, "RESTORE_PREPARED")
            with self.assertRaises(RecoveryLedgerError):
                writer.initialize(record, self._initialization(record.backup_digest))
            self.assertIsNone(writer.read())
        finally:
            store.close()
            temporary.cleanup()

    def test_owned_append_does_not_reacquire_quiescence(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-owner-no-nested-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            controller = WalSidecarController(root, busy_timeout_ms=100)
            session = controller.hold_quiescence()
            try:
                owner = session.issue_owner()
                writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
                record = self._record(1, "RESTORE_PREPARED")
                with mock.patch.object(
                    WalSidecarController,
                    "hold_quiescence",
                    side_effect=AssertionError("owned append must not reacquire"),
                ):
                    writer.initialize_owned(
                        record,
                        self._initialization(record.backup_digest),
                        owner=owner,
                    )
            finally:
                session.close()


class RecoveryTombstoneTest(unittest.TestCase):
    @staticmethod
    def _identity(operation_id: str, effect_key: str) -> RestoreIdentity:
        return RestoreIdentity(operation_id=operation_id, effect_key=effect_key)

    @staticmethod
    def _previous_hwm() -> dict[str, int]:
        return {
            "previous_recovery_epoch": 0,
            "previous_fencing_token_hwm": 0,
            "previous_last_clock_ns": 0,
        }

    @classmethod
    def _record(
        cls,
        sequence: int,
        phase: TombstonePhase,
        *,
        generation: int = 1,
        identities: tuple[RestoreIdentity, ...] = (),
    ) -> RecoveryTombstoneRecord:
        return RecoveryTombstoneRecord(
            version=1,
            sequence=sequence,
            phase=phase,
            restore_generation=generation,
            backup_digest="sha256:" + "a" * 64,
            previous_primary_digest="sha256:" + "b" * 64,
            candidate_digest="sha256:" + "c" * 64,
            previous_recovery_epoch=0,
            previous_fencing_token_hwm=0,
            previous_last_clock_ns=0,
            identities=identities,
            actor="operator",
            audit_ref=f"audit/tombstone/{generation}",
        )

    @classmethod
    def _normal_open_history(
        cls,
        second_terminal: TombstonePhase | None,
    ) -> tuple[tuple[RecoveryLedgerRecord, ...], tuple[RecoveryTombstoneRecord, ...]]:
        first_identity = cls._identity("op-a", "effect-a")
        first_ledger = (
            RecoveryLedgerWriterTest._record(1, "RESTORE_PREPARED"),
            RecoveryLedgerWriterTest._record(2, "RESTORE_REPLACED"),
            RecoveryLedgerWriterTest._record(3, "RESTORE_COMMITTED"),
        )
        first_tombstones = (
            replace(
                cls._record(1, "PREPARED", identities=(first_identity,)),
                audit_ref="audit/1",
            ),
            replace(
                cls._record(2, "COMMITTED", identities=(first_identity,)),
                audit_ref="audit/1",
            ),
        )
        if second_terminal is None:
            return first_ledger, first_tombstones
        second_identity = cls._identity("op-b", "effect-b")
        if second_terminal == "COMMITTED":
            second_ledger: tuple[RecoveryLedgerRecord, ...] = (
                RecoveryLedgerWriterTest._record(
                    4,
                    "RESTORE_PREPARED",
                    generation=2,
                ),
                RecoveryLedgerWriterTest._record(
                    5,
                    "RESTORE_REPLACED",
                    generation=2,
                ),
                RecoveryLedgerWriterTest._record(
                    6,
                    "RESTORE_COMMITTED",
                    generation=2,
                ),
            )
        else:
            second_ledger = (
                RecoveryLedgerWriterTest._record(
                    4,
                    "RESTORE_PREPARED",
                    generation=2,
                ),
                RecoveryLedgerWriterTest._record(
                    5,
                    "RESTORE_ABORTED",
                    generation=2,
                ),
            )
        second_tombstones = (
            replace(
                cls._record(
                    3,
                    "PREPARED",
                    generation=2,
                    identities=(second_identity,),
                ),
                audit_ref="audit/2",
            ),
            replace(
                cls._record(
                    4,
                    second_terminal,
                    generation=2,
                    identities=(second_identity,),
                ),
                audit_ref="audit/2",
            ),
        )
        return (
            (*first_ledger, *second_ledger),
            (*first_tombstones, *second_tombstones),
        )

    def test_tombstone_append_rejects_same_generation_actor_or_audit_changes(
        self,
    ) -> None:
        for field_name, changed_value in (
            ("actor", "different-actor"),
            ("audit_ref", "audit/different"),
        ):
            with (
                self.subTest(field_name=field_name),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-tombstone-provenance-"
                ) as temporary,
            ):
                root = _root(temporary)
                tombstones = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
                first = self._record(1, "PREPARED")
                second = replace(
                    self._record(2, "COMMITTED"),
                    actor=changed_value if field_name == "actor" else first.actor,
                    audit_ref=(
                        changed_value if field_name == "audit_ref" else first.audit_ref
                    ),
                )
                tombstones.initialize(
                    first,
                    _issue_recovery_ledger_initialization(
                        operator_id=first.actor,
                        audit_ref=first.audit_ref,
                        request_digest=first.backup_digest,
                    ),
                )
                with self.assertRaises(RecoveryLedgerError):
                    tombstones.append(second)

    def test_tombstone_parser_rejects_nonadjacent_restore_generation(self) -> None:
        prepared = self._record(1, "PREPARED", generation=1)
        committed = self._record(2, "COMMITTED", generation=1)
        skipped = self._record(3, "PREPARED", generation=3)
        raw = b"".join(
            recovery_module._encode_tombstone(record)
            for record in (prepared, committed, skipped)
        )
        with self.assertRaises(RecoveryLedgerError):
            recovery_module._latest_tombstone(raw, allow_empty=False)

    def test_tombstone_append_rejects_nonadjacent_restore_generation_before_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-tombstone-generation-gap-"
        ) as temporary:
            root = _root(temporary)
            tombstones = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            prepared = self._record(1, "PREPARED", generation=1)
            committed = self._record(2, "COMMITTED", generation=1)
            skipped = self._record(3, "PREPARED", generation=3)
            authority = _issue_recovery_ledger_initialization(
                operator_id=prepared.actor,
                audit_ref=prepared.audit_ref,
                request_digest=prepared.backup_digest,
            )
            tombstones.initialize(prepared, authority)
            tombstones.append(committed)
            before = (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes()
            with self.assertRaises(RecoveryLedgerError):
                tombstones.append(skipped)
            self.assertEqual(
                before,
                (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes(),
            )

    def test_prepare_persists_previous_destination_hwm_in_handle_and_tombstones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-previous-hwm-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = restore_ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(self._identity("op-a", "effect-a"),),
                        actor="operator",
                        audit_ref="audit/restore/previous-hwm",
                        previous_recovery_epoch=5,
                        previous_fencing_token_hwm=10,
                        previous_last_clock_ns=123,
                        floor_lower_bound=RecoveryFloor(6, 11),
                        owner=owner,
                    )
                    self.assertEqual(5, prepared.previous_recovery_epoch)
                    self.assertEqual(10, prepared.previous_fencing_token_hwm)
                    self.assertEqual(123, prepared.previous_last_clock_ns)
                    replaced = restore_ledger.mark_replaced(
                        prepared,
                        floor=RecoveryFloor(6, 11),
                        owner=owner,
                    )
                    committed = restore_ledger.mark_committed(
                        replaced,
                        floor=RecoveryFloor(6, 11),
                        owner=owner,
                    )
                    reconstructed = restore_ledger.read(owner=owner)
                    self.assertIsNotNone(reconstructed)
                    assert reconstructed is not None
                    self.assertEqual(5, reconstructed.previous_recovery_epoch)
                    self.assertEqual(10, reconstructed.previous_fencing_token_hwm)
                    self.assertEqual(123, reconstructed.previous_last_clock_ns)
                    second = restore_ledger.prepare(
                        backup_digest="sha256:" + "d" * 64,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        identities=(self._identity("op-b", "effect-b"),),
                        actor="operator",
                        audit_ref="audit/restore/previous-hwm-abort",
                        previous_recovery_epoch=6,
                        previous_fencing_token_hwm=11,
                        previous_last_clock_ns=124,
                        floor_lower_bound=RecoveryFloor(7, 12),
                        owner=owner,
                    )
                    aborted = restore_ledger.mark_aborted(
                        second,
                        floor=RecoveryFloor(7, 12),
                        owner=owner,
                    )
                self.assertEqual(5, committed.previous_recovery_epoch)
                self.assertEqual(10, committed.previous_fencing_token_hwm)
                self.assertEqual(123, committed.previous_last_clock_ns)
                self.assertEqual(6, aborted.previous_recovery_epoch)
                self.assertEqual(11, aborted.previous_fencing_token_hwm)
                self.assertEqual(124, aborted.previous_last_clock_ns)
                records = [
                    json.loads(line)
                    for line in (root / RECOVERY_TOMBSTONES_BASENAME)
                    .read_text()
                    .splitlines()
                ]
                self.assertEqual(4, len(records))
                for record in records[:2]:
                    self.assertEqual(5, record["previous_recovery_epoch"])
                    self.assertEqual(10, record["previous_fencing_token_hwm"])
                    self.assertEqual(123, record["previous_last_clock_ns"])
                for record in records[2:]:
                    self.assertEqual(6, record["previous_recovery_epoch"])
                    self.assertEqual(11, record["previous_fencing_token_hwm"])
                    self.assertEqual(124, record["previous_last_clock_ns"])
            finally:
                owner.close()

    def test_prepare_requires_target_floor_above_previous_destination_hwm(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-previous-hwm-floor-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    for floor in (
                        RecoveryFloor(5, 11),
                        RecoveryFloor(6, 10),
                    ):
                        with self.assertRaises(RecoveryLedgerError):
                            restore_ledger.prepare(
                                backup_digest="sha256:" + "a" * 64,
                                previous_primary_digest="sha256:" + "b" * 64,
                                candidate_digest="sha256:" + "c" * 64,
                                identities=(self._identity("op-a", "effect-a"),),
                                actor="operator",
                                audit_ref="audit/restore/previous-hwm-floor",
                                previous_recovery_epoch=5,
                                previous_fencing_token_hwm=10,
                                previous_last_clock_ns=123,
                                floor_lower_bound=floor,
                                owner=owner,
                            )
            finally:
                owner.close()

    def test_tombstone_previous_destination_hwm_fields_are_exact_nonnegative_ints(
        self,
    ) -> None:
        values = {
            "previous_recovery_epoch": 5,
            "previous_fencing_token_hwm": 10,
            "previous_last_clock_ns": 123,
        }
        for field_name, invalid_value in (
            ("previous_recovery_epoch", True),
            ("previous_fencing_token_hwm", -1),
            ("previous_last_clock_ns", 2**63),
        ):
            fields = dict(values)
            fields[field_name] = invalid_value
            with self.assertRaises((TypeError, ValueError)):
                RecoveryTombstoneRecord(
                    version=1,
                    sequence=1,
                    phase="PREPARED",
                    restore_generation=1,
                    backup_digest="sha256:" + "a" * 64,
                    previous_primary_digest="sha256:" + "b" * 64,
                    candidate_digest="sha256:" + "c" * 64,
                    previous_recovery_epoch=fields["previous_recovery_epoch"],
                    previous_fencing_token_hwm=fields["previous_fencing_token_hwm"],
                    previous_last_clock_ns=fields["previous_last_clock_ns"],
                    identities=(),
                    actor="operator",
                    audit_ref="audit/restore/previous-hwm-fields",
                )

    def test_tombstone_reader_rejects_previous_destination_hwm_corruption(self) -> None:
        for field_name, invalid_value in (
            ("previous_recovery_epoch", -1),
            ("previous_fencing_token_hwm", 2**63),
            ("previous_last_clock_ns", True),
        ):
            with tempfile.TemporaryDirectory(
                prefix="agent-team-restore-previous-hwm-corrupt-"
            ) as temporary:
                root = _root(temporary)
                log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
                first = self._record(
                    1,
                    "PREPARED",
                    identities=(self._identity("op-a", "effect-a"),),
                )
                second = self._record(
                    2,
                    "COMMITTED",
                    identities=(self._identity("op-a", "effect-a"),),
                )
                log.initialize(
                    first,
                    _issue_recovery_ledger_initialization(
                        operator_id=first.actor,
                        audit_ref=first.audit_ref,
                        request_digest=first.backup_digest,
                    ),
                )
                log.append(second)
                path = root / RECOVERY_TOMBSTONES_BASENAME
                lines = path.read_bytes().splitlines()
                terminal = cast(dict[str, object], json.loads(lines[-1]))
                terminal[field_name] = invalid_value
                path.write_bytes(
                    b"\n".join(
                        [
                            *lines[:-1],
                            json.dumps(
                                terminal,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                        ]
                    )
                    + b"\n"
                )
                with self.assertRaises(RecoveryLedgerError):
                    log.read()

    def test_persisted_hwm_relation_rejects_equal_lower_and_overflow_history(
        self,
    ) -> None:
        cases = (
            ("previous_recovery_epoch", 1),
            ("previous_recovery_epoch", 2),
            ("previous_fencing_token_hwm", 1),
            ("previous_fencing_token_hwm", 2),
            ("previous_last_clock_ns", 2**63),
        )
        for field_name, invalid_value in cases:
            with tempfile.TemporaryDirectory(
                prefix="agent-team-restore-hwm-relation-"
            ) as temporary:
                root = _root(temporary)
                owner = BorrowedRootOwner(root)
                restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                floor = RecoveryFloor(1, 1)
                try:
                    with mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ):
                        prepared = restore_ledger.prepare(
                            backup_digest="sha256:" + "a" * 64,
                            previous_primary_digest="sha256:" + "b" * 64,
                            candidate_digest="sha256:" + "c" * 64,
                            identities=(self._identity("op-a", "effect-a"),),
                            actor="operator",
                            audit_ref="audit/restore/hwm-relation",
                            **self._previous_hwm(),
                            floor_lower_bound=floor,
                            owner=owner,
                        )
                        replaced = restore_ledger.mark_replaced(prepared, floor, owner)
                        committed = restore_ledger.mark_committed(
                            replaced, floor, owner
                        )
                    path = root / RECOVERY_TOMBSTONES_BASENAME
                    lines = path.read_bytes().splitlines()
                    mutated_lines: list[bytes] = []
                    for line in lines:
                        record = cast(dict[str, object], json.loads(line))
                        record[field_name] = invalid_value
                        mutated_lines.append(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                    path.write_bytes(b"\n".join(mutated_lines) + b"\n")
                    root_fd = os.open(
                        root,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                        | os.O_CLOEXEC,
                    )
                    try:
                        with mock.patch(
                            "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                        ):
                            operations = (
                                lambda restore_ledger=restore_ledger, owner=owner: (
                                    restore_ledger.read(owner=owner)
                                ),
                                lambda restore_ledger=restore_ledger, owner=owner: (
                                    restore_ledger.read_owned(owner=owner)
                                ),
                                lambda restore_ledger=restore_ledger, floor=floor, owner=owner: (
                                    restore_ledger.prepare(
                                        backup_digest="sha256:" + "a" * 64,
                                        previous_primary_digest="sha256:" + "b" * 64,
                                        candidate_digest="sha256:" + "c" * 64,
                                        identities=(
                                            self._identity("op-a", "effect-a"),
                                        ),
                                        actor="operator",
                                        audit_ref="audit/restore/hwm-relation",
                                        **self._previous_hwm(),
                                        floor_lower_bound=floor,
                                        owner=owner,
                                    )
                                ),
                                lambda restore_ledger=restore_ledger, committed=committed, floor=floor, owner=owner: (
                                    restore_ledger.mark_replaced(
                                        committed, floor, owner
                                    )
                                ),
                                lambda restore_ledger=restore_ledger, committed=committed, floor=floor, owner=owner: (
                                    restore_ledger.mark_committed(
                                        committed, floor, owner
                                    )
                                ),
                                lambda restore_ledger=restore_ledger, committed=committed, floor=floor, owner=owner: (
                                    restore_ledger.mark_aborted(committed, floor, owner)
                                ),
                                lambda restore_ledger=restore_ledger, committed=committed, owner=owner: (
                                    restore_ledger.verify_generation(committed, owner)
                                ),
                                lambda restore_ledger=restore_ledger, owner=owner: (
                                    restore_ledger.active_committed_identities(
                                        owner=owner
                                    )
                                ),
                                lambda root_fd=root_fd: _normal_open_preflight(root_fd),
                            )
                            for operation in operations:
                                with self.assertRaises(RecoveryLedgerError):
                                    operation()
                    finally:
                        os.close(root_fd)
                finally:
                    owner.close()

    def test_orphan_prepare_rejects_floor_not_above_supplied_previous_hwm(self) -> None:
        for orphan_kind in ("ledger", "tombstone"):
            with tempfile.TemporaryDirectory(
                prefix="agent-team-restore-hwm-orphan-"
            ) as temporary:
                root = _root(temporary)
                backup_digest = "sha256:" + "a" * 64
                if orphan_kind == "ledger":
                    RecoveryLedgerWriter(root, marker_name=MARKER_NAME).initialize(
                        RecoveryLedgerWriterTest._record(1, "RESTORE_PREPARED"),
                        RecoveryLedgerWriterTest._initialization(backup_digest),
                    )
                else:
                    tombstone = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest=backup_digest,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=1,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(self._identity("op-a", "effect-a"),),
                        actor="operator",
                        audit_ref="audit/restore/hwm-orphan",
                    )
                    RecoveryTombstoneLog(root, marker_name=MARKER_NAME).initialize(
                        tombstone,
                        _issue_recovery_ledger_initialization(
                            operator_id=tombstone.actor,
                            audit_ref=tombstone.audit_ref,
                            request_digest=tombstone.backup_digest,
                        ),
                    )
                owner = BorrowedRootOwner(root)
                ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                try:
                    with (
                        mock.patch(
                            "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                        ),
                        self.assertRaises(RecoveryLedgerError),
                    ):
                        ledger.prepare(
                            backup_digest=backup_digest,
                            previous_primary_digest="sha256:" + "b" * 64,
                            candidate_digest="sha256:" + "c" * 64,
                            identities=(self._identity("op-a", "effect-a"),),
                            actor="operator",
                            audit_ref="audit/restore/hwm-orphan",
                            previous_recovery_epoch=1,
                            previous_fencing_token_hwm=0,
                            previous_last_clock_ns=0,
                            floor_lower_bound=RecoveryFloor(1, 1),
                            owner=owner,
                        )
                finally:
                    owner.close()

    def test_ledger_only_orphan_checks_existing_floor_before_tombstone_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-ledger-only-hwm-"
        ) as temporary:
            root = _root(temporary)
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            RecoveryLedgerWriter(root, marker_name=MARKER_NAME).initialize(
                RecoveryLedgerWriterTest._record(1, "RESTORE_PREPARED"),
                RecoveryLedgerWriterTest._initialization(),
            )
            ledger_before = ledger_path.read_bytes()
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    restore_ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                        previous_recovery_epoch=5,
                        previous_fencing_token_hwm=5,
                        previous_last_clock_ns=0,
                        floor_lower_bound=RecoveryFloor(6, 6),
                        owner=owner,
                    )
                self.assertEqual(ledger_before, ledger_path.read_bytes())
                self.assertFalse(tombstone_path.exists())
            finally:
                owner.close()

    def test_prepare_checks_hwm_before_any_orphan_or_pair_append(self) -> None:
        branches = (
            "both_absent",
            "ledger_only",
            "tombstone_only",
            "matching_pair",
            "mismatching_pair",
        )
        hwm_modes = ("valid", "equal", "lower")
        backup_digest = "sha256:" + "a" * 64
        previous_primary_digest = "sha256:" + "b" * 64
        candidate_digest = "sha256:" + "c" * 64
        identity = self._identity("op-a", "effect-a")

        def install_branch(
            root: Path,
            branch: str,
            hwm_mode: str,
        ) -> None:
            if branch == "both_absent":
                return
            with CoordinationStore(root):
                pass
            ledger_writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            tombstone_log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            ledger_record = RecoveryLedgerRecord(
                version=1,
                sequence=1,
                phase="RESTORE_PREPARED",
                restore_generation=1,
                recovery_epoch=1,
                fencing_token_floor=1,
                backup_digest=backup_digest,
                actor="operator",
                audit_ref="audit/1",
            )
            if branch in {
                "ledger_only",
                "matching_pair",
                "mismatching_pair",
            }:
                ledger_writer.initialize(
                    ledger_record,
                    RecoveryLedgerWriterTest._initialization(backup_digest),
                )
                if branch in {"matching_pair", "mismatching_pair"}:
                    ledger_writer.append(
                        RecoveryLedgerRecord(
                            version=1,
                            sequence=2,
                            phase="RESTORE_REPLACED",
                            restore_generation=1,
                            recovery_epoch=1,
                            fencing_token_floor=1,
                            backup_digest=backup_digest,
                            actor="operator",
                            audit_ref="audit/1",
                        )
                    )
                    ledger_writer.append(
                        RecoveryLedgerRecord(
                            version=1,
                            sequence=3,
                            phase="RESTORE_COMMITTED",
                            restore_generation=1,
                            recovery_epoch=1,
                            fencing_token_floor=1,
                            backup_digest=backup_digest,
                            actor="operator",
                            audit_ref="audit/1",
                        )
                    )
            if branch in {
                "tombstone_only",
                "matching_pair",
                "mismatching_pair",
            }:
                previous_hwm = {
                    "valid": (0, 0),
                    "equal": (1, 1),
                    "lower": (2, 2),
                }[hwm_mode]
                tombstone_backup = (
                    backup_digest
                    if branch != "mismatching_pair"
                    else "sha256:" + "d" * 64
                )
                tombstone = RecoveryTombstoneRecord(
                    version=1,
                    sequence=1,
                    phase="PREPARED",
                    restore_generation=1,
                    backup_digest=tombstone_backup,
                    previous_primary_digest=previous_primary_digest,
                    candidate_digest=candidate_digest,
                    previous_recovery_epoch=previous_hwm[0],
                    previous_fencing_token_hwm=previous_hwm[1],
                    previous_last_clock_ns=0,
                    identities=(identity,),
                    actor="operator",
                    audit_ref="audit/1",
                )
                tombstone_log.initialize(
                    tombstone,
                    _issue_recovery_ledger_initialization(
                        operator_id="operator",
                        audit_ref="audit/1",
                        request_digest=tombstone_backup,
                    ),
                )
                if branch in {"matching_pair", "mismatching_pair"}:
                    tombstone_log.append(
                        RecoveryTombstoneRecord(
                            version=1,
                            sequence=2,
                            phase="COMMITTED",
                            restore_generation=1,
                            backup_digest=tombstone_backup,
                            previous_primary_digest=previous_primary_digest,
                            candidate_digest=candidate_digest,
                            previous_recovery_epoch=previous_hwm[0],
                            previous_fencing_token_hwm=previous_hwm[1],
                            previous_last_clock_ns=0,
                            identities=(identity,),
                            actor="operator",
                            audit_ref="audit/1",
                        )
                    )

        for branch in branches:
            for hwm_mode in hwm_modes:
                with tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-hwm-branches-"
                ) as temporary:
                    root = _root(temporary)
                    install_branch(root, branch, hwm_mode)
                    ledger_path = root / RECOVERY_LEDGER_BASENAME
                    tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                    ledger_before = (
                        ledger_path.read_bytes() if ledger_path.exists() else None
                    )
                    tombstone_before = (
                        tombstone_path.read_bytes() if tombstone_path.exists() else None
                    )
                    owner = BorrowedRootOwner(root)
                    restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                    hwm = {
                        "valid": (0, 0),
                        "equal": (1, 1),
                        "lower": (2, 2),
                    }[hwm_mode]
                    floor_value = (
                        (1, 1)
                        if branch in {"both_absent", "tombstone_only"}
                        else (2, 2)
                    )
                    if hwm_mode == "lower" and branch in {
                        "ledger_only",
                        "matching_pair",
                        "mismatching_pair",
                    }:
                        floor_value = (3, 3)
                    try:
                        with mock.patch(
                            "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                        ):
                            succeeded = False
                            try:
                                restore_ledger.prepare(
                                    backup_digest=backup_digest,
                                    previous_primary_digest=previous_primary_digest,
                                    candidate_digest=candidate_digest,
                                    identities=(identity,),
                                    actor="operator",
                                    audit_ref="audit/1",
                                    previous_recovery_epoch=hwm[0],
                                    previous_fencing_token_hwm=hwm[1],
                                    previous_last_clock_ns=0,
                                    floor_lower_bound=RecoveryFloor(*floor_value),
                                    owner=owner,
                                )
                            except RecoveryLedgerError:
                                pass
                            else:
                                succeeded = True
                            self.assertEqual(
                                hwm_mode == "valid" and branch != "mismatching_pair",
                                succeeded,
                            )
                        if hwm_mode != "valid" or branch == "mismatching_pair":
                            self.assertEqual(
                                ledger_before,
                                ledger_path.read_bytes()
                                if ledger_path.exists()
                                else None,
                            )
                            self.assertEqual(
                                tombstone_before,
                                tombstone_path.read_bytes()
                                if tombstone_path.exists()
                                else None,
                            )
                    finally:
                        owner.close()

    def test_tombstone_writer_is_strict_and_owner_aware(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-tombstone-") as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            prepared = self._record(
                1,
                "PREPARED",
                identities=(
                    self._identity("op-a", "effect-a"),
                    self._identity("op-b", "effect-b"),
                ),
            )
            committed = self._record(
                2,
                "COMMITTED",
                identities=prepared.identities,
            )
            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    mock.patch(
                        "agent_team.recovery.StateFilesystem.open_existing",
                        side_effect=AssertionError(
                            "owner path reacquired StateFilesystem"
                        ),
                    ),
                ):
                    log.initialize_owned(prepared, owner=owner)
                    self.assertEqual(prepared, log.read_owned(owner=owner))
                    log.append_owned(committed, owner=owner)
                    self.assertEqual(committed, log.read_owned(owner=owner))
                lines = (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes().splitlines()
                self.assertEqual(2, len(lines))
                for line in lines:
                    self.assertEqual(
                        line,
                        json.dumps(
                            json.loads(line),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode(),
                    )
            finally:
                owner.close()

    def test_real_quiescence_owner_keeps_gate_and_marker_held_during_append(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-real-owner-") as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger_writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first_ledger = RecoveryLedgerWriterTest._record(1, "RESTORE_PREPARED")
            second_ledger = RecoveryLedgerWriterTest._record(2, "RESTORE_REPLACED")
            ledger_writer.initialize(
                first_ledger,
                _issue_recovery_ledger_initialization(
                    operator_id="operator",
                    audit_ref="audit/1",
                    request_digest=first_ledger.backup_digest,
                ),
            )
            tombstone_log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            first_tombstone = self._record(1, "PREPARED")
            second_tombstone = self._record(2, "COMMITTED")
            tombstone_log.initialize(
                first_tombstone,
                _issue_recovery_ledger_initialization(
                    operator_id="operator",
                    audit_ref=first_tombstone.audit_ref,
                    request_digest=first_tombstone.backup_digest,
                ),
            )
            controller = WalSidecarController(root)
            with controller.hold_quiescence() as session:
                owner = session.issue_owner()
                with mock.patch(
                    "agent_team.recovery.StateFilesystem.open_existing",
                    side_effect=AssertionError("owner path reacquired StateFilesystem"),
                ):
                    ledger_writer.append_owned(second_ledger, owner=owner)
                    tombstone_log.append_owned(second_tombstone, owner=owner)
                session.assert_identity()

    def test_tombstone_rejects_unsorted_duplicate_and_future_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-tombstone-") as temporary:
            root = _root(temporary)
            path = root / RECOVERY_TOMBSTONES_BASENAME
            unsorted = {
                "version": 1,
                "sequence": 1,
                "phase": "PREPARED",
                "restore_generation": 1,
                "backup_digest": "sha256:" + "a" * 64,
                "previous_primary_digest": "sha256:" + "b" * 64,
                "candidate_digest": "sha256:" + "c" * 64,
                "previous_recovery_epoch": 0,
                "previous_fencing_token_hwm": 0,
                "previous_last_clock_ns": 0,
                "identities": [
                    {"operation_id": "op-b", "effect_key": "effect-b"},
                    {"operation_id": "op-a", "effect_key": "effect-a"},
                ],
                "actor": "operator",
                "audit_ref": "audit/tombstone/1",
            }
            path.write_bytes(
                json.dumps(unsorted, separators=(",", ":")).encode() + b"\n"
            )
            with self.assertRaises(RecoveryRequiredError):
                RecoveryTombstoneLog(root, marker_name=MARKER_NAME).read()
            path.write_bytes(
                b'{"version":1,"sequence":1,"phase":"PREPARED",'
                b'"restore_generation":1,"backup_digest":"sha256:'
                + b"a" * 64
                + b'","previous_primary_digest":"sha256:'
                + b"b" * 64
                + b'","candidate_digest":"sha256:'
                + b"c" * 64
                + b'","previous_recovery_epoch":0,"previous_fencing_token_hwm":0,'
                + b'"previous_last_clock_ns":0,"identities":[{"operation_id":"op-a",'
                b'"effect_key":"effect-a"},{"operation_id":"op-a",'
                b'"effect_key":"effect-a"}],"actor":"operator",'
                b'"audit_ref":"audit/tombstone/1"}\n'
            )
            with self.assertRaises(RecoveryRequiredError):
                RecoveryTombstoneLog(root, marker_name=MARKER_NAME).read()

    def test_restore_ledger_derives_generation_sequence_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-ledger-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            identities = (self._identity("op-a", "effect-a"),)
            backup_digest = "sha256:" + "a" * 64
            previous_primary_digest = "sha256:" + "b" * 64
            candidate_digest = "sha256:" + "c" * 64
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    mock.patch(
                        "agent_team.recovery.StateFilesystem.open_existing",
                        side_effect=AssertionError(
                            "owner path reacquired StateFilesystem"
                        ),
                    ),
                ):
                    prepared = ledger.prepare(
                        backup_digest=backup_digest,
                        previous_primary_digest=previous_primary_digest,
                        candidate_digest=candidate_digest,
                        identities=identities,
                        actor="operator",
                        audit_ref="audit/restore/1",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    repeated = ledger.prepare(
                        backup_digest=backup_digest,
                        previous_primary_digest=previous_primary_digest,
                        candidate_digest=candidate_digest,
                        identities=identities,
                        actor="operator",
                        audit_ref="audit/restore/1",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    self.assertEqual(prepared, repeated)
                    replaced = ledger.mark_replaced(
                        prepared,
                        floor=floor,
                        owner=owner,
                    )
                    self.assertEqual(
                        replaced,
                        ledger.mark_replaced(
                            replaced,
                            floor=floor,
                            owner=owner,
                        ),
                    )
                    committed = ledger.mark_committed(
                        replaced,
                        floor=floor,
                        owner=owner,
                    )
                    self.assertEqual(
                        committed,
                        ledger.mark_committed(
                            committed,
                            floor=floor,
                            owner=owner,
                        ),
                    )
                self.assertEqual("RESTORE_COMMITTED", committed.phase)
                self.assertEqual(1, committed.restore_generation)
                self.assertEqual(3, committed.sequence)
                self.assertEqual(
                    3,
                    len((root / RECOVERY_LEDGER_BASENAME).read_bytes().splitlines()),
                )
                self.assertEqual(
                    2,
                    len(
                        (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes().splitlines()
                    ),
                )
            finally:
                owner.close()

    def test_restore_commit_resumes_after_tombstone_response_loss(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-resume-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            identity = self._identity("op-a", "effect-a")
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/resume",
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    replaced = ledger.mark_replaced(
                        prepared,
                        floor=floor,
                        owner=owner,
                    )
                    tombstone = RecoveryTombstoneRecord(
                        version=1,
                        sequence=2,
                        phase="COMMITTED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/resume",
                    )
                    ledger.tombstones.append_owned(tombstone, owner=owner)
                    committed = ledger.mark_committed(
                        replaced,
                        floor=floor,
                        owner=owner,
                    )
                self.assertEqual("RESTORE_COMMITTED", committed.phase)
                self.assertEqual(
                    3,
                    len((root / RECOVERY_LEDGER_BASENAME).read_bytes().splitlines()),
                )
                self.assertEqual(
                    2,
                    len(
                        (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes().splitlines()
                    ),
                )
            finally:
                owner.close()

    def test_restore_new_generation_preserves_committed_identity_union(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-generation-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            first_identity = self._identity("op-a", "effect-a")
            second_identity = self._identity("op-b", "effect-b")
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    first = ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(first_identity,),
                        actor="operator",
                        audit_ref="audit/restore/generation-1",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    first_replaced = ledger.mark_replaced(
                        first,
                        floor=floor,
                        owner=owner,
                    )
                    first_committed = ledger.mark_committed(
                        first_replaced,
                        floor=floor,
                        owner=owner,
                    )
                    second = ledger.prepare(
                        backup_digest="sha256:" + "d" * 64,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        identities=(second_identity,),
                        actor="operator",
                        audit_ref="audit/restore/generation-2",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    self.assertEqual(2, second.restore_generation)
                    self.assertEqual(4, second.sequence)
                    aborted = ledger.mark_aborted(
                        second,
                        floor=floor,
                        owner=owner,
                    )
                    self.assertEqual("RESTORE_ABORTED", aborted.phase)
                    self.assertEqual(
                        frozenset({("op-a", "effect-a")}),
                        ledger.active_committed_identities(owner=owner),
                    )
                    self.assertEqual(
                        frozenset({("op-a", "effect-a")}),
                        _normal_open_preflight(
                            owner._root_fd
                        ).active_committed_identities(),
                    )
                self.assertEqual("RESTORE_COMMITTED", first_committed.phase)
            finally:
                owner.close()

    def test_normal_open_preflight_checks_every_ledger_generation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-preflight-history-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger_writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            generation_one = RecoveryLedgerWriterTest._record(
                1, "RESTORE_PREPARED", generation=1
            )
            ledger_writer.initialize(
                generation_one,
                RecoveryLedgerWriterTest._initialization(generation_one.backup_digest),
            )
            ledger_writer.append(
                RecoveryLedgerWriterTest._record(2, "RESTORE_REPLACED", generation=1)
            )
            ledger_writer.append(
                RecoveryLedgerWriterTest._record(3, "RESTORE_COMMITTED", generation=1)
            )
            generation_two = RecoveryLedgerRecord(
                version=1,
                sequence=4,
                phase="RESTORE_PREPARED",
                restore_generation=2,
                recovery_epoch=2,
                fencing_token_floor=2,
                backup_digest="sha256:" + "d" * 64,
                actor="operator",
                audit_ref="audit/4",
            )
            ledger_writer.append(generation_two)
            ledger_writer.append(
                RecoveryLedgerRecord(
                    version=1,
                    sequence=5,
                    phase="RESTORE_REPLACED",
                    restore_generation=2,
                    recovery_epoch=2,
                    fencing_token_floor=2,
                    backup_digest=generation_two.backup_digest,
                    actor="operator",
                    audit_ref="audit/4",
                )
            )
            ledger_writer.append(
                RecoveryLedgerRecord(
                    version=1,
                    sequence=6,
                    phase="RESTORE_COMMITTED",
                    restore_generation=2,
                    recovery_epoch=2,
                    fencing_token_floor=2,
                    backup_digest=generation_two.backup_digest,
                    actor="operator",
                    audit_ref="audit/4",
                )
            )
            tombstone_log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            first_identity = self._identity("oldop", "oldeffect")
            second_identity = self._identity("newop", "neweffect")

            def tombstone(
                sequence: int,
                phase: TombstonePhase,
                generation: int,
                backup_digest: str,
                audit_ref: str,
                identity: RestoreIdentity,
            ) -> RecoveryTombstoneRecord:
                return RecoveryTombstoneRecord(
                    version=1,
                    sequence=sequence,
                    phase=phase,
                    restore_generation=generation,
                    backup_digest=backup_digest,
                    previous_primary_digest="sha256:" + "b" * 64,
                    candidate_digest="sha256:" + "c" * 64,
                    previous_recovery_epoch=0,
                    previous_fencing_token_hwm=0,
                    previous_last_clock_ns=0,
                    identities=(identity,),
                    actor="operator",
                    audit_ref=audit_ref,
                )

            first_tombstone = tombstone(
                1,
                "PREPARED",
                1,
                generation_one.backup_digest,
                "audit/1",
                first_identity,
            )
            tombstone_log.initialize(
                first_tombstone,
                RecoveryLedgerWriterTest._initialization(first_tombstone.backup_digest),
            )
            tombstone_log.append(
                tombstone(
                    2,
                    "ABORTED",
                    1,
                    generation_one.backup_digest,
                    "audit/1",
                    first_identity,
                )
            )
            second_tombstone = tombstone(
                3,
                "PREPARED",
                2,
                generation_two.backup_digest,
                "audit/4",
                second_identity,
            )
            tombstone_log.append(second_tombstone)
            tombstone_log.append(
                tombstone(
                    4,
                    "COMMITTED",
                    2,
                    generation_two.backup_digest,
                    "audit/4",
                    second_identity,
                )
            )
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                with self.assertRaises(RecoveryRequiredError):
                    _normal_open_preflight(root_fd)
            finally:
                os.close(root_fd)

    def test_owner_restore_operations_validate_all_prior_generations(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-owner-history-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    first = restore_ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(self._identity("oldop", "oldeffect"),),
                        actor="operator",
                        audit_ref="audit/restore/owner-first",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    first = restore_ledger.mark_replaced(first, floor, owner)
                    restore_ledger.mark_committed(first, floor, owner)
                    second = restore_ledger.prepare(
                        backup_digest="sha256:" + "d" * 64,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        identities=(self._identity("newop", "neweffect"),),
                        actor="operator",
                        audit_ref="audit/restore/owner-second",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    second = restore_ledger.mark_replaced(second, floor, owner)
                    second = restore_ledger.mark_committed(second, floor, owner)

                tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                lines = tombstone_path.read_bytes().splitlines()
                terminal = cast(dict[str, object], json.loads(lines[1]))
                terminal["phase"] = "ABORTED"
                tombstone_path.write_bytes(
                    b"\n".join(
                        [
                            *lines[:1],
                            json.dumps(
                                terminal,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                            *lines[2:],
                        ]
                    )
                    + b"\n"
                )
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.read(owner=owner)
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.read_owned(owner=owner)
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.prepare(
                            backup_digest="sha256:" + "d" * 64,
                            previous_primary_digest="sha256:" + "e" * 64,
                            candidate_digest="sha256:" + "f" * 64,
                            identities=(self._identity("newop", "neweffect"),),
                            actor="operator",
                            audit_ref="audit/restore/owner-second",
                            **self._previous_hwm(),
                            floor_lower_bound=floor,
                            owner=owner,
                        )
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.verify_generation(second, owner=owner)
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.active_committed_identities(owner=owner)
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.mark_replaced(second, floor, owner)
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.mark_committed(second, floor, owner)
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.mark_aborted(second, floor, owner)
            finally:
                owner.close()

    def test_active_identity_union_rejects_prepared_ledger_with_terminal_tombstone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-owner-phase-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            identity = self._identity("op-a", "effect-a")
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = restore_ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/phase-mismatch",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    committed_tombstone = RecoveryTombstoneRecord(
                        version=1,
                        sequence=2,
                        phase="COMMITTED",
                        restore_generation=prepared.restore_generation,
                        backup_digest=prepared.backup_digest,
                        previous_primary_digest=prepared.previous_primary_digest,
                        candidate_digest=prepared.candidate_digest,
                        previous_recovery_epoch=prepared.previous_recovery_epoch,
                        previous_fencing_token_hwm=prepared.previous_fencing_token_hwm,
                        previous_last_clock_ns=prepared.previous_last_clock_ns,
                        identities=prepared.identities,
                        actor=prepared.actor,
                        audit_ref=prepared.audit_ref,
                    )
                    restore_ledger.tombstones.append_owned(
                        committed_tombstone,
                        owner=owner,
                    )
                    with self.assertRaises(RecoveryLedgerError):
                        restore_ledger.active_committed_identities(owner=owner)
            finally:
                owner.close()

    def test_restore_entrypoints_use_exhaustive_phase_pair_map(self) -> None:
        ledger_phases = (
            "RESTORE_PREPARED",
            "RESTORE_REPLACED",
            "RESTORE_COMMITTED",
            "RESTORE_ABORTED",
        )
        tombstone_phases = ("PREPARED", "COMMITTED", "ABORTED")
        allowed_pairs = {
            ("RESTORE_PREPARED", "PREPARED"),
            ("RESTORE_PREPARED", "ABORTED"),
            ("RESTORE_REPLACED", "PREPARED"),
            ("RESTORE_REPLACED", "COMMITTED"),
            ("RESTORE_COMMITTED", "COMMITTED"),
            ("RESTORE_ABORTED", "ABORTED"),
        }
        entrypoints = (
            "read",
            "read_owned",
            "prepare",
            "mark_replaced",
            "mark_committed",
            "mark_aborted",
            "verify_generation",
            "active_committed_identities",
        )
        backup_digest = "sha256:" + "a" * 64
        previous_primary_digest = "sha256:" + "b" * 64
        candidate_digest = "sha256:" + "c" * 64
        identity = self._identity("op-a", "effect-a")
        floor = RecoveryFloor(1, 1)

        def install_pair(
            root: Path,
            ledger_phase: str,
            tombstone_phase: str,
        ) -> RestoreHandle:
            with CoordinationStore(root):
                pass
            ledger_writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            tombstone_log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            prepared_ledger = RecoveryLedgerRecord(
                version=1,
                sequence=1,
                phase="RESTORE_PREPARED",
                restore_generation=1,
                recovery_epoch=1,
                fencing_token_floor=1,
                backup_digest=backup_digest,
                actor="operator",
                audit_ref="audit/restore/phase-map",
            )
            prepared_tombstone = RecoveryTombstoneRecord(
                version=1,
                sequence=1,
                phase="PREPARED",
                restore_generation=1,
                backup_digest=backup_digest,
                previous_primary_digest=previous_primary_digest,
                candidate_digest=candidate_digest,
                previous_recovery_epoch=0,
                previous_fencing_token_hwm=0,
                previous_last_clock_ns=0,
                identities=(identity,),
                actor="operator",
                audit_ref="audit/restore/phase-map",
            )
            authority = _issue_recovery_ledger_initialization(
                operator_id="operator",
                audit_ref="audit/restore/phase-map",
                request_digest=backup_digest,
            )
            ledger_writer.initialize(prepared_ledger, authority)
            tombstone_log.initialize(prepared_tombstone, authority)
            latest_ledger = prepared_ledger
            latest_tombstone = prepared_tombstone
            if ledger_phase in {"RESTORE_REPLACED", "RESTORE_COMMITTED"}:
                latest_ledger = RecoveryLedgerRecord(
                    version=1,
                    sequence=2,
                    phase="RESTORE_REPLACED",
                    restore_generation=1,
                    recovery_epoch=1,
                    fencing_token_floor=1,
                    backup_digest=backup_digest,
                    actor="operator",
                    audit_ref="audit/restore/phase-map",
                )
                ledger_writer.append(latest_ledger)
            if ledger_phase == "RESTORE_COMMITTED":
                latest_ledger = RecoveryLedgerRecord(
                    version=1,
                    sequence=3,
                    phase="RESTORE_COMMITTED",
                    restore_generation=1,
                    recovery_epoch=1,
                    fencing_token_floor=1,
                    backup_digest=backup_digest,
                    actor="operator",
                    audit_ref="audit/restore/phase-map",
                )
                ledger_writer.append(latest_ledger)
            elif ledger_phase == "RESTORE_ABORTED":
                latest_ledger = RecoveryLedgerRecord(
                    version=1,
                    sequence=2,
                    phase="RESTORE_ABORTED",
                    restore_generation=1,
                    recovery_epoch=1,
                    fencing_token_floor=1,
                    backup_digest=backup_digest,
                    actor="operator",
                    audit_ref="audit/restore/phase-map",
                )
                ledger_writer.append(latest_ledger)
            if tombstone_phase in {"COMMITTED", "ABORTED"}:
                latest_tombstone = RecoveryTombstoneRecord(
                    version=1,
                    sequence=2,
                    phase=cast(TombstonePhase, tombstone_phase),
                    restore_generation=1,
                    backup_digest=backup_digest,
                    previous_primary_digest=previous_primary_digest,
                    candidate_digest=candidate_digest,
                    previous_recovery_epoch=0,
                    previous_fencing_token_hwm=0,
                    previous_last_clock_ns=0,
                    identities=(identity,),
                    actor="operator",
                    audit_ref="audit/restore/phase-map",
                )
                tombstone_log.append(latest_tombstone)
            try:
                return _issue_restore_handle(latest_ledger, latest_tombstone)
            except RecoveryLedgerError:
                return _issue_restore_handle(prepared_ledger, prepared_tombstone)

        def invoke(
            entrypoint: str,
            ledger: RestoreLedger,
            handle: RestoreHandle,
            owner: BorrowedRootOwner,
        ) -> object:
            if entrypoint == "read":
                return ledger.read(owner=owner)
            if entrypoint == "read_owned":
                return ledger.read_owned(owner=owner)
            if entrypoint == "prepare":
                return ledger.prepare(
                    backup_digest=backup_digest,
                    previous_primary_digest=previous_primary_digest,
                    candidate_digest=candidate_digest,
                    identities=(identity,),
                    actor="operator",
                    audit_ref="audit/restore/phase-map",
                    **self._previous_hwm(),
                    floor_lower_bound=floor,
                    owner=owner,
                )
            if entrypoint == "mark_replaced":
                return ledger.mark_replaced(handle, floor, owner)
            if entrypoint == "mark_committed":
                return ledger.mark_committed(handle, floor, owner)
            if entrypoint == "mark_aborted":
                return ledger.mark_aborted(handle, floor, owner)
            if entrypoint == "verify_generation":
                return ledger.verify_generation(handle, owner)
            if entrypoint == "active_committed_identities":
                return ledger.active_committed_identities(owner=owner)
            raise AssertionError(f"unknown restore entrypoint: {entrypoint}")

        for ledger_phase, tombstone_phase in product(ledger_phases, tombstone_phases):
            for entrypoint in entrypoints:
                with tempfile.TemporaryDirectory(
                    prefix="agent-team-restore-phase-map-"
                ) as temporary:
                    root = _root(temporary)
                    handle = install_pair(root, ledger_phase, tombstone_phase)
                    owner = BorrowedRootOwner(root)
                    ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                    try:
                        with mock.patch(
                            "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                        ):
                            error: RecoveryLedgerError | None = None
                            try:
                                invoke(entrypoint, ledger, handle, owner)
                            except RecoveryLedgerError as caught:
                                error = caught
                            if (ledger_phase, tombstone_phase) in allowed_pairs:
                                if error is not None:
                                    self.assertNotEqual(
                                        "current restore pair is inconsistent",
                                        str(error),
                                    )
                            else:
                                self.assertIsNotNone(
                                    error,
                                    f"{entrypoint} accepted invalid phase pair "
                                    f"{ledger_phase}+{tombstone_phase}",
                                )
                    finally:
                        owner.close()

    def test_unowned_tombstone_append_rejects_root_swap(self) -> None:
        class RootSwapTombstoneLog(RecoveryTombstoneLog):
            swapped = False

            def _fault(self, point: str) -> None:
                if point != "before_final_check" or self.swapped:
                    return
                self.swapped = True
                old_root = self.state_root.with_name("state-old")
                self.state_root.rename(old_root)
                self.state_root.mkdir(mode=0o700)

        with tempfile.TemporaryDirectory(
            prefix="agent-team-tombstone-root-swap-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            identity = self._identity("op-a", "effect-a")
            first = self._record(1, "PREPARED", identities=(identity,))
            second = self._record(2, "COMMITTED", identities=(identity,))
            log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            log.initialize(
                first,
                _issue_recovery_ledger_initialization(
                    operator_id=first.actor,
                    audit_ref=first.audit_ref,
                    request_digest=first.backup_digest,
                ),
            )
            swapping_log = RootSwapTombstoneLog(root, marker_name=MARKER_NAME)
            with self.assertRaises(RecoveryLedgerError):
                swapping_log.append(second)

    def test_prepare_resumes_missing_next_generation_ledger_record(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-resume-ledger-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            first_floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            second_floor = RecoveryFloor(recovery_epoch=2, fencing_token_floor=2)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    first = restore_ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(self._identity("oldop", "oldeffect"),),
                        actor="operator",
                        audit_ref="audit/restore/first",
                        **self._previous_hwm(),
                        floor_lower_bound=first_floor,
                        owner=owner,
                    )
                    first = restore_ledger.mark_replaced(first, first_floor, owner)
                    restore_ledger.mark_committed(first, first_floor, owner)
                    original_append = RecoveryLedgerWriter._append_owned_at_root

                    def fail_generation_two(
                        writer: RecoveryLedgerWriter,
                        root_fd: int,
                        record: RecoveryLedgerRecord,
                        *,
                        allow_create: bool,
                    ) -> RecoveryLedgerRecord:
                        if record.restore_generation == 2:
                            raise RecoveryLedgerError("simulated missing ledger append")
                        return original_append(
                            writer,
                            root_fd,
                            record,
                            allow_create=allow_create,
                        )

                    with (
                        mock.patch.object(
                            RecoveryLedgerWriter,
                            "_append_owned_at_root",
                            new=fail_generation_two,
                        ),
                        self.assertRaises(RecoveryLedgerError),
                    ):
                        restore_ledger.prepare(
                            backup_digest="sha256:" + "d" * 64,
                            previous_primary_digest="sha256:" + "e" * 64,
                            candidate_digest="sha256:" + "f" * 64,
                            identities=(self._identity("newop", "neweffect"),),
                            actor="operator",
                            audit_ref="audit/restore/second",
                            **self._previous_hwm(),
                            floor_lower_bound=second_floor,
                            owner=owner,
                        )
                    resumed = restore_ledger.prepare(
                        backup_digest="sha256:" + "d" * 64,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(self._identity("newop", "neweffect"),),
                        actor="operator",
                        audit_ref="audit/restore/second",
                        floor_lower_bound=second_floor,
                        owner=owner,
                    )
                self.assertEqual(2, resumed.restore_generation)
                self.assertEqual(4, resumed.sequence)
            finally:
                owner.close()

    def test_prepare_blocks_next_generation_when_prior_pair_disagrees(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-resume-history-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    first = restore_ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(self._identity("oldop", "oldeffect"),),
                        actor="operator",
                        audit_ref="audit/restore/history-first",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    first = restore_ledger.mark_replaced(first, floor, owner)
                    restore_ledger.mark_committed(first, floor, owner)
                owner.close()

                tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                lines = tombstone_path.read_bytes().splitlines()
                terminal = cast(dict[str, object], json.loads(lines[-1]))
                terminal["phase"] = "ABORTED"
                tombstone_path.write_bytes(
                    b"\n".join(
                        [
                            *lines[:-1],
                            json.dumps(
                                terminal,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                        ]
                    )
                    + b"\n"
                )
                tombstone_log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
                tombstone_log.append(
                    RecoveryTombstoneRecord(
                        version=1,
                        sequence=3,
                        phase="PREPARED",
                        restore_generation=2,
                        backup_digest="sha256:" + "d" * 64,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(self._identity("newop", "neweffect"),),
                        actor="operator",
                        audit_ref="audit/restore/history-second",
                    )
                )

                owner = BorrowedRootOwner(root)
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    restore_ledger.prepare(
                        backup_digest="sha256:" + "d" * 64,
                        previous_primary_digest="sha256:" + "e" * 64,
                        candidate_digest="sha256:" + "f" * 64,
                        identities=(self._identity("newop", "neweffect"),),
                        actor="operator",
                        audit_ref="audit/restore/history-second",
                        **self._previous_hwm(),
                        floor_lower_bound=RecoveryFloor(2, 2),
                        owner=owner,
                    )
            finally:
                owner.close()

    def test_restore_abort_resumes_after_tombstone_response_loss(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-abort-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
            identity = self._identity("op-a", "effect-a")
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/abort",
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    aborted_tombstone = RecoveryTombstoneRecord(
                        version=1,
                        sequence=2,
                        phase="ABORTED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/abort",
                    )
                    ledger.tombstones.append_owned(
                        aborted_tombstone,
                        owner=owner,
                    )
                    aborted = ledger.mark_aborted(
                        prepared,
                        floor=floor,
                        owner=owner,
                    )
                self.assertEqual("RESTORE_ABORTED", aborted.phase)
                self.assertEqual(1, aborted.restore_generation)
            finally:
                owner.close()

    def test_restore_handle_carries_exact_ledger_floor_across_resume(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-floor-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            first_floor = RecoveryFloor(recovery_epoch=7, fencing_token_floor=11)
            second_floor = RecoveryFloor(recovery_epoch=8, fencing_token_floor=12)
            third_floor = RecoveryFloor(recovery_epoch=9, fencing_token_floor=13)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(self._identity("op-a", "effect-a"),),
                        actor="operator",
                        audit_ref="audit/restore/floor",
                        **self._previous_hwm(),
                        floor_lower_bound=first_floor,
                        owner=owner,
                    )
                    self.assertEqual(7, getattr(prepared, "recovery_epoch", None))
                    self.assertEqual(
                        11,
                        getattr(prepared, "fencing_token_floor", None),
                    )
                    replaced = ledger.mark_replaced(
                        prepared,
                        floor=second_floor,
                        owner=owner,
                    )
                    self.assertEqual(8, getattr(replaced, "recovery_epoch", None))
                    self.assertEqual(
                        12,
                        getattr(replaced, "fencing_token_floor", None),
                    )
                    committed = ledger.mark_committed(
                        replaced,
                        floor=third_floor,
                        owner=owner,
                    )
                    self.assertEqual(9, getattr(committed, "recovery_epoch", None))
                    self.assertEqual(
                        13,
                        getattr(committed, "fencing_token_floor", None),
                    )
                    resumed = ledger.verify_generation(committed, owner)
                    self.assertEqual(9, resumed.recovery_epoch)
                    self.assertEqual(13, resumed.fencing_token_floor)
            finally:
                owner.close()

    def test_restore_resume_rejects_forged_or_stale_handle_floor(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-floor-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            floor = RecoveryFloor(recovery_epoch=7, fencing_token_floor=11)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    handle = ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(self._identity("op-a", "effect-a"),),
                        actor="operator",
                        audit_ref="audit/restore/forged-floor",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    forged = object.__new__(type(handle))
                    fields = (
                        "restore_generation",
                        "sequence",
                        "tombstone_sequence",
                        "phase",
                        "tombstone_phase",
                        "recovery_epoch",
                        "fencing_token_floor",
                        "backup_digest",
                        "previous_primary_digest",
                        "candidate_digest",
                        "previous_recovery_epoch",
                        "previous_fencing_token_hwm",
                        "previous_last_clock_ns",
                        "identities",
                        "actor",
                        "audit_ref",
                        "_provenance",
                    )
                    try:
                        for field_name in fields:
                            object.__setattr__(
                                forged,
                                field_name,
                                object.__getattribute__(handle, field_name),
                            )
                    except AttributeError:
                        self.fail("RestoreHandle must carry exact ledger floor fields")
                    object.__setattr__(
                        forged,
                        "previous_fencing_token_hwm",
                        handle.previous_fencing_token_hwm + 1,
                    )
                    with self.assertRaises(RecoveryLedgerError):
                        ledger.verify_generation(forged, owner)
            finally:
                owner.close()

    def test_restore_ledger_accepts_store_issued_restore_identities(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-identity-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root) as store:
                store.create_intent(
                    "store-op",
                    effect_key="store-effect",
                    provider_id="provider/test",
                    actor="operator",
                    clock_ns=100,
                )
                _checkpoint(store)
                database_fd = store._database_fd
                assert database_fd is not None
                store_identities = store._read_restore_identities(database_fd)
            self.assertEqual(1, len(store_identities))
            self.assertIs(LeaseRestoreIdentity, type(store_identities[0]))
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    try:
                        prepared = ledger.prepare(
                            backup_digest="sha256:" + "a" * 64,
                            previous_primary_digest="sha256:" + "b" * 64,
                            candidate_digest="sha256:" + "c" * 64,
                            identities=store_identities,
                            actor="operator",
                            audit_ref="audit/restore/store-identity",
                            **self._previous_hwm(),
                            floor_lower_bound=RecoveryFloor(1, 1),
                            owner=owner,
                        )
                    except (TypeError, ValueError, RecoveryLedgerError) as exc:
                        self.fail(
                            f"Store-issued RestoreIdentity must cross the seam: {exc}"
                        )
                self.assertEqual(1, prepared.restore_generation)
                self.assertEqual(store_identities, prepared.identities)
            finally:
                owner.close()

    def test_normal_open_preflight_returns_typed_immutable_state(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-typed-state-"
        ) as temporary:
            root = _root(temporary)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                state = _normal_open_preflight(root_fd)
                self.assertEqual((), state.active_committed_tombstones)
                self.assertIsNone(state.latest_committed_handle)
                self.assertEqual(frozenset(), state.active_committed_identities())
            finally:
                os.close(root_fd)

    def test_normal_open_state_is_issuer_only_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-state-issuer-"
        ) as temporary:
            root = _root(temporary)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                state = _normal_open_preflight(root_fd)
                with self.assertRaises(TypeError):
                    type(state)()
                with self.assertRaises(FrozenInstanceError):
                    state.latest_committed_handle = None  # type: ignore[misc]
                with self.assertRaises(TypeError):
                    state.__copy__()
                forged = object.__new__(type(state))
                object.__setattr__(
                    forged,
                    "active_committed_tombstones",
                    (),
                )
                object.__setattr__(forged, "latest_committed_handle", None)
                object.__setattr__(forged, "_provenance", object())
                with self.assertRaises(RecoveryLedgerError):
                    recovery_module._validate_normal_open_recovery_state(forged)
                with self.assertRaises(RecoveryLedgerError):
                    recovery_module._validate_normal_open_recovery_state(object())
            finally:
                os.close(root_fd)

    def test_normal_open_state_tracks_committed_union_and_latest_handle(self) -> None:
        ledger_records, tombstone_records = self._normal_open_history("COMMITTED")
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-committed-history-"
        ) as temporary:
            root = _root(temporary)
            _write_recovery_pair(root, ledger_records, tombstone_records)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                state = _normal_open_preflight(root_fd)
                self.assertEqual(
                    (
                        ("op-a", "effect-a"),
                        ("op-b", "effect-b"),
                    ),
                    tuple(
                        (identity.operation_id, identity.effect_key)
                        for identity in state.active_committed_tombstones
                    ),
                )
                latest = state.latest_committed_handle
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(2, latest.restore_generation)
                self.assertEqual("RESTORE_COMMITTED", latest.phase)
                self.assertEqual(
                    frozenset(
                        {
                            ("op-a", "effect-a"),
                            ("op-b", "effect-b"),
                        }
                    ),
                    state.active_committed_identities(),
                )
            finally:
                os.close(root_fd)

    def test_normal_open_state_excludes_aborted_latest_generation(self) -> None:
        ledger_records, tombstone_records = self._normal_open_history("ABORTED")
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-aborted-latest-"
        ) as temporary:
            root = _root(temporary)
            _write_recovery_pair(root, ledger_records, tombstone_records)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                state = _normal_open_preflight(root_fd)
                self.assertEqual(
                    (("op-a", "effect-a"),),
                    tuple(
                        (identity.operation_id, identity.effect_key)
                        for identity in state.active_committed_tombstones
                    ),
                )
                latest = state.latest_committed_handle
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(1, latest.restore_generation)
                self.assertEqual("RESTORE_COMMITTED", latest.phase)
            finally:
                os.close(root_fd)

    def test_normal_open_state_aborted_only_is_empty(self) -> None:
        identity = self._identity("op-aborted", "effect-aborted")
        ledger_records = (
            RecoveryLedgerWriterTest._record(1, "RESTORE_PREPARED"),
            RecoveryLedgerWriterTest._record(2, "RESTORE_ABORTED"),
        )
        tombstone_records = (
            replace(
                self._record(1, "PREPARED", identities=(identity,)),
                audit_ref="audit/1",
            ),
            replace(
                self._record(2, "ABORTED", identities=(identity,)),
                audit_ref="audit/1",
            ),
        )
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-aborted-only-"
        ) as temporary:
            root = _root(temporary)
            _write_recovery_pair(root, ledger_records, tombstone_records)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                state = _normal_open_preflight(root_fd)
                self.assertEqual((), state.active_committed_tombstones)
                self.assertIsNone(state.latest_committed_handle)
            finally:
                os.close(root_fd)

    def test_normal_open_state_changes_for_canonical_past_identity_tamper(
        self,
    ) -> None:
        ledger_records, tombstone_records = self._normal_open_history("COMMITTED")
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-tamper-"
        ) as temporary:
            root = _root(temporary)
            _write_recovery_pair(root, ledger_records, tombstone_records)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            try:
                before = _normal_open_preflight(root_fd)
                lines = tombstone_path.read_bytes().splitlines()
                forged_identity = {
                    "operation_id": "op-forged",
                    "effect_key": "effect-forged",
                }
                changed_lines: list[bytes] = []
                for line in lines:
                    item = cast(dict[str, object], json.loads(line))
                    item["identities"] = [forged_identity]
                    changed_lines.append(
                        json.dumps(
                            item,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                tombstone_path.write_bytes(b"\n".join(changed_lines) + b"\n")
                after = _normal_open_preflight(root_fd)
                self.assertNotEqual(
                    before.active_committed_tombstones,
                    after.active_committed_tombstones,
                )
                self.assertEqual(
                    (("op-forged", "effect-forged"),),
                    tuple(
                        (identity.operation_id, identity.effect_key)
                        for identity in after.active_committed_tombstones
                    ),
                )
            finally:
                os.close(root_fd)

    def test_normal_open_preflight_is_read_only_and_does_not_lock(self) -> None:
        ledger_records, tombstone_records = self._normal_open_history("COMMITTED")
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-read-only-"
        ) as temporary:
            root = _root(temporary)
            _write_recovery_pair(root, ledger_records, tombstone_records)
            before = (
                (root / RECOVERY_LEDGER_BASENAME).read_bytes(),
                (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes(),
            )
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                with (
                    mock.patch("agent_team.recovery.os.fsync") as fsync,
                    mock.patch("agent_team.recovery.fcntl.flock") as flock,
                ):
                    state = _normal_open_preflight(root_fd)
                self.assertEqual(2, len(state.active_committed_tombstones))
                fsync.assert_not_called()
                flock.assert_not_called()
                self.assertEqual(
                    before,
                    (
                        (root / RECOVERY_LEDGER_BASENAME).read_bytes(),
                        (root / RECOVERY_TOMBSTONES_BASENAME).read_bytes(),
                    ),
                )
            finally:
                os.close(root_fd)

    def test_normal_open_state_owner_uses_pair_durability_barrier(self) -> None:
        ledger_records, tombstone_records = self._normal_open_history("COMMITTED")
        with tempfile.TemporaryDirectory(
            prefix="agent-team-normal-open-owner-barrier-"
        ) as temporary:
            root = _root(temporary)
            _write_recovery_pair(root, ledger_records, tombstone_records)
            owner = BorrowedRootOwner(root)
            try:
                restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner",
                        BorrowedRootOwner,
                    ),
                    mock.patch.object(
                        StateFilesystem,
                        "open_existing",
                        side_effect=AssertionError(
                            "normal open owner path must not reacquire filesystem"
                        ),
                    ),
                    mock.patch("agent_team.recovery.os.fsync", wraps=os.fsync) as fsync,
                ):
                    state = restore_ledger.normal_open_state(owner)
                self.assertEqual(2, len(state.active_committed_tombstones))
                self.assertIsNotNone(state.latest_committed_handle)
                self.assertGreaterEqual(fsync.call_count, 3)
            finally:
                owner.close()

    def test_normal_open_preflight_returns_only_committed_tombstones(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-team-preflight-") as temporary:
            root = _root(temporary)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            try:
                self.assertEqual(
                    frozenset(),
                    _normal_open_preflight(root_fd).active_committed_identities(),
                )
                owner = BorrowedRootOwner(root)
                ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                identity = self._identity("op-a", "effect-a")
                backup_digest = "sha256:" + "a" * 64
                previous_primary_digest = "sha256:" + "b" * 64
                candidate_digest = "sha256:" + "c" * 64
                floor = RecoveryFloor(recovery_epoch=1, fencing_token_floor=1)
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = ledger.prepare(
                        backup_digest=backup_digest,
                        previous_primary_digest=previous_primary_digest,
                        candidate_digest=candidate_digest,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/preflight",
                        **self._previous_hwm(),
                        floor_lower_bound=floor,
                        owner=owner,
                    )
                    with self.assertRaises(RecoveryRequiredError):
                        _normal_open_preflight(root_fd)
                    replaced = ledger.mark_replaced(
                        prepared,
                        floor=floor,
                        owner=owner,
                    )
                    with self.assertRaises(RecoveryRequiredError):
                        _normal_open_preflight(root_fd)
                    committed = ledger.mark_committed(
                        replaced,
                        floor=floor,
                        owner=owner,
                    )
                self.assertEqual(
                    frozenset({("op-a", "effect-a")}),
                    _normal_open_preflight(root_fd).active_committed_identities(),
                )
                self.assertEqual("RESTORE_COMMITTED", committed.phase)
                owner.close()
            finally:
                os.close(root_fd)

    def test_normal_open_preflight_rejects_a_single_recovery_file(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-preflight-pair-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = RecoveryLedgerWriterTest._record(1, "RESTORE_PREPARED")
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    writer.initialize_owned(
                        first,
                        RecoveryLedgerWriterTest._initialization(first.backup_digest),
                        owner=owner,
                    )
                    root_fd = owner._root_fd
                    with self.assertRaises(RecoveryRequiredError):
                        _normal_open_preflight(root_fd)
            finally:
                owner.close()

    def test_normal_open_preflight_rejects_tombstone_request_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-preflight-tombstone-request-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            identity = self._identity("op-a", "effect-a")
            try:
                with mock.patch(
                    "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                ):
                    prepared = ledger.prepare(
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        identities=(identity,),
                        actor="operator",
                        audit_ref="audit/restore/request-mutation",
                        **self._previous_hwm(),
                        floor_lower_bound=RecoveryFloor(1, 1),
                        owner=owner,
                    )
                    replaced = ledger.mark_replaced(
                        prepared,
                        floor=RecoveryFloor(1, 1),
                        owner=owner,
                    )
                    ledger.mark_committed(
                        replaced,
                        floor=RecoveryFloor(1, 1),
                        owner=owner,
                    )
                tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                lines = tombstone_path.read_bytes().splitlines()
                terminal = cast(dict[str, object], json.loads(lines[-1]))
                terminal["identities"] = [
                    {"operation_id": "mutated-op", "effect_key": "mutated-effect"}
                ]
                tombstone_path.write_bytes(
                    b"\n".join(
                        [
                            *lines[:-1],
                            json.dumps(
                                terminal,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                        ]
                    )
                    + b"\n"
                )
                root_fd = os.open(
                    root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | os.O_CLOEXEC,
                )
                try:
                    with self.assertRaises(RecoveryRequiredError):
                        _normal_open_preflight(root_fd)
                finally:
                    os.close(root_fd)
            finally:
                owner.close()


class RecoveryLedgerWriterContinuationTest(RecoveryLedgerWriterTest):
    def test_open_failure_transfers_cleanup_capability_for_both_recovery_logs(
        self,
    ) -> None:
        """A malformed marker must not discard partial filesystem cleanup."""

        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-open-cleanup-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                marker = root / MARKER_NAME
                marker.write_bytes(b"malformed marker")
                gate_fds: list[int] = []
                real_open = os.open
                real_close = os.close
                real_flock = fcntl.flock

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _gate_fds: list[int] = gate_fds,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] == store_module.LIFETIME_GATE_FILENAME:
                        _gate_fds.append(fd)
                    return fd

                def fail_gate_close(
                    fd: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_close: Callable[[int], None] = real_close,
                ) -> None:
                    if fd in _gate_fds:
                        raise OSError("persistent gate close failure")
                    _real_close(fd)

                def fail_gate_unlock(
                    fd: int,
                    operation: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_flock: Callable[[int, int], None] = real_flock,
                ) -> None:
                    if fd in _gate_fds and operation == fcntl.LOCK_UN:
                        raise OSError("persistent gate unlock failure")
                    _real_flock(fd, operation)

                log = log_factory(root, marker_name=MARKER_NAME)
                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.os.close", side_effect=fail_gate_close
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock",
                        side_effect=fail_gate_unlock,
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    log.read()
                error = raised.exception
                self.assertIsNotNone(error.cleanup_owner)
                self.assertEqual(1, len(gate_fds))

                marker.write_bytes(store_module.WRITER_MARKER_CLEAN_CONTENT)
                error.retry_cleanup()
                error.retry_cleanup()
                self.assertIsNone(error.cleanup_owner)
                self.assertIsNone(log.read())
                with WalSidecarController(root).hold_quiescence():
                    pass

    def test_open_failure_cleanup_capability_handles_actual_close_and_reuse(
        self,
    ) -> None:
        """Retry must tolerate an actual close and refuse a reused descriptor."""

        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-open-cleanup-reuse-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                marker = root / MARKER_NAME
                marker.write_bytes(b"malformed marker")
                real_open = os.open
                real_close = os.close
                real_flock = fcntl.flock
                gate_fds: list[int] = []

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _gate_fds: list[int] = gate_fds,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] == store_module.LIFETIME_GATE_FILENAME:
                        _gate_fds.append(fd)
                    return fd

                def fail_after_actual_gate_close(
                    fd: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_close: Callable[[int], None] = real_close,
                ) -> None:
                    if fd in _gate_fds:
                        _real_close(fd)
                        raise OSError("close status is unknown after actual close")
                    _real_close(fd)

                def pass_flock(
                    fd: int,
                    operation: int,
                    _real_flock: Callable[[int, int], None] = real_flock,
                ) -> None:
                    _real_flock(fd, operation)

                log = log_factory(root, marker_name=MARKER_NAME)
                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=fail_after_actual_gate_close,
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock", side_effect=pass_flock
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    log.read()
                error = raised.exception
                self.assertIsNotNone(error.cleanup_owner)
                self.assertEqual(1, len(gate_fds))
                marker.write_bytes(store_module.WRITER_MARKER_CLEAN_CONTENT)
                error.retry_cleanup()
                self.assertIsNone(error.cleanup_owner)
                self.assertIsNone(log.read())

                marker.write_bytes(b"malformed marker")
                gate_fds.clear()

                def fail_gate_close(
                    fd: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_close: Callable[[int], None] = real_close,
                ) -> None:
                    if fd in _gate_fds:
                        raise OSError("persistent gate close failure")
                    _real_close(fd)

                def fail_gate_unlock(
                    fd: int,
                    operation: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_flock: Callable[[int, int], None] = real_flock,
                ) -> None:
                    if fd in _gate_fds and operation == fcntl.LOCK_UN:
                        raise OSError("persistent gate unlock failure")
                    _real_flock(fd, operation)

                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.os.close", side_effect=fail_gate_close
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock",
                        side_effect=fail_gate_unlock,
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    log.read()
                error = raised.exception
                self.assertIsNotNone(error.cleanup_owner)
                old_gate_fd = gate_fds[0]
                real_close(old_gate_fd)
                replacement_path = root / "descriptor-reuse-target"
                replacement_fds: list[int] = []
                replacement_fd = -1
                for _ in range(16):
                    candidate_fd = real_open(
                        replacement_path, os.O_CREAT | os.O_RDWR, 0o600
                    )
                    replacement_fds.append(candidate_fd)
                    if candidate_fd == old_gate_fd:
                        replacement_fd = candidate_fd
                        break
                self.assertEqual(old_gate_fd, replacement_fd)
                try:
                    with self.assertRaises(RuntimeError):
                        error.retry_cleanup()
                    os.fstat(replacement_fd)
                    for fd in replacement_fds:
                        try:
                            real_close(fd)
                        except OSError:
                            pass
                    with self.assertRaises(RuntimeError):
                        error.retry_cleanup()
                    self.assertIsNotNone(error.cleanup_owner)
                finally:
                    for fd in replacement_fds:
                        try:
                            real_close(fd)
                        except OSError:
                            pass

    def test_pre_session_acquisition_cleanup_is_retained_and_drained_for_both_logs(
        self,
    ) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-pre-session-cleanup-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                if log_factory is RecoveryLedgerWriter:
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(1, "RESTORE_PREPARED")
                    )
                    authority = self._initialization(record.backup_digest)
                else:
                    record = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                    )
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                log = log_factory(root, busy_timeout_ms=50)
                real_open = os.open
                real_close = os.close
                real_flock = fcntl.flock
                held_fds: list[int] = []

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _held_fds: list[int] = held_fds,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] in {
                        store_module.LIFETIME_GATE_FILENAME,
                        MARKER_NAME,
                    }:
                        _held_fds.append(fd)
                    return fd

                def fail_held_close(
                    fd: int,
                    _held_fds: list[int] = held_fds,
                    _real_close: Callable[[int], None] = real_close,
                ) -> None:
                    if fd in _held_fds:
                        raise OSError("persistent pre-session close failure")
                    _real_close(fd)

                def fail_held_unlock(
                    fd: int,
                    operation: int,
                    _held_fds: list[int] = held_fds,
                    _real_flock: Callable[[int, int], None] = real_flock,
                ) -> None:
                    if fd in _held_fds and operation == fcntl.LOCK_UN:
                        raise OSError("persistent pre-session unlock failure")
                    _real_flock(fd, operation)

                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.os.close", side_effect=fail_held_close
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock",
                        side_effect=fail_held_unlock,
                    ),
                    mock.patch.object(
                        QuiescenceSession,
                        "_issue",
                        side_effect=RuntimeError("session issuance failed"),
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    if isinstance(record, RecoveryLedgerRecord):
                        assert isinstance(log, RecoveryLedgerWriter)
                        log.initialize(record, authority)
                    else:
                        assert isinstance(log, RecoveryTombstoneLog)
                        log.initialize(record, authority)
                self.assertIn(
                    "recovery quiescence is unavailable", str(raised.exception)
                )
                self.assertIsNotNone(raised.exception.cleanup_owner)
                self.assertEqual(1, len(log._orphan_controllers))
                raised.exception.retry_cleanup()
                self.assertIsNone(raised.exception.cleanup_owner)
                self.assertEqual([], log._orphan_controllers)
                self.assertEqual([], log._orphan_sessions)
                self.assertFalse(
                    (
                        root
                        / (
                            RECOVERY_LEDGER_BASENAME
                            if isinstance(log, RecoveryLedgerWriter)
                            else RECOVERY_TOMBSTONES_BASENAME
                        )
                    ).exists()
                )
                self.assertIsNone(log.read())
                self.assertEqual([], log._orphan_controllers)
                with WalSidecarController(root).hold_quiescence():
                    pass
                if isinstance(log, RecoveryLedgerWriter):
                    assert isinstance(record, RecoveryLedgerRecord)
                    self.assertEqual(record, log.initialize(record, authority))
                else:
                    assert isinstance(record, RecoveryTombstoneRecord)
                    self.assertEqual(record, log.initialize(record, authority))

    def test_body_exception_precedes_context_and_read_cleanup_for_both_logs(
        self,
    ) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-body-cleanup-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                log = log_factory(root, marker_name=MARKER_NAME)
                if isinstance(log, RecoveryLedgerWriter):
                    first = self._record(1, "RESTORE_PREPARED")
                    log.initialize(first, self._initialization(first.backup_digest))
                ledger_name = (
                    RECOVERY_LEDGER_BASENAME
                    if log_factory is RecoveryLedgerWriter
                    else RECOVERY_TOMBSTONES_BASENAME
                )
                body_error = RecoveryLedgerError("body failure")
                context_filesystem = StateFilesystem(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=ledger_name,
                    busy_timeout_ms=0,
                )
                with (
                    mock.patch.object(
                        StateFilesystem,
                        "close",
                        side_effect=OSError("context cleanup failure"),
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                    log,
                ):
                    log._orphan_filesystems.append(context_filesystem)
                    raise body_error
                self.assertIs(body_error, raised.exception)
                self.assertIsNotNone(raised.exception.__cause__)
                self.assertIn(
                    "filesystem resources cannot be closed",
                    str(raised.exception.__cause__),
                )
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                self.assertIsNone(raised.exception.cleanup_owner)
                log.close()

                read_body = RecoveryLedgerError("read body failure")
                with mock.patch.object(
                    StateFilesystem,
                    "close",
                    side_effect=OSError("read cleanup failure"),
                ):
                    if log_factory is RecoveryLedgerWriter:
                        read_patch = mock.patch.object(
                            log,
                            "_read_ledger",
                            side_effect=read_body,
                        )
                    else:
                        read_patch = mock.patch.object(
                            recovery_module,
                            "_read_root_file",
                            side_effect=read_body,
                        )
                    with read_patch, self.assertRaises(RecoveryLedgerError) as raised:
                        log.read()
                self.assertIs(read_body, raised.exception)
                self.assertIsNotNone(raised.exception.__cause__)
                self.assertIn(
                    "filesystem close status is unknown",
                    str(raised.exception.__cause__),
                )
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                self.assertIsNone(raised.exception.cleanup_owner)
                log.close()

                no_body_filesystem = StateFilesystem(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=ledger_name,
                    busy_timeout_ms=0,
                )
                with (
                    mock.patch.object(
                        StateFilesystem,
                        "close",
                        side_effect=OSError("successful body cleanup failure"),
                    ),
                    self.assertRaises(RecoveryLedgerError),
                    log,
                ):
                    log._orphan_filesystems.append(no_body_filesystem)
                log.close()

    def test_temporary_read_cleanup_chains_read_error_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-temporary-read-cleanup-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                if log_factory is RecoveryLedgerWriter:
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(1, "RESTORE_PREPARED")
                    )
                    authority = self._initialization(record.backup_digest)
                    target_name = RECOVERY_LEDGER_BASENAME
                else:
                    record = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                    )
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                    target_name = RECOVERY_TOMBSTONES_BASENAME
                log = log_factory(root, marker_name=MARKER_NAME)
                if isinstance(log, RecoveryLedgerWriter):
                    assert isinstance(record, RecoveryLedgerRecord)
                    log.initialize(record, authority)
                else:
                    assert isinstance(record, RecoveryTombstoneRecord)
                    log.initialize(record, authority)
                root_fd = os.open(
                    root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | os.O_CLOEXEC,
                )
                real_open = os.open
                real_read = os.read
                real_close = os.close
                read_fds: list[int] = []

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _read_fds: list[int] = read_fds,
                    _target_name: str = target_name,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] == _target_name:
                        _read_fds.append(fd)
                    return fd

                def fail_read(
                    fd: int,
                    count: int,
                    _real_read: Callable[[int, int], bytes] = real_read,
                    _read_fds: list[int] = read_fds,
                ) -> bytes:
                    if fd in _read_fds:
                        raise OSError("temporary read body failure")
                    return _real_read(fd, count)

                def fail_close(
                    fd: int,
                    _real_close: Callable[[int], None] = real_close,
                    _read_fds: list[int] = read_fds,
                ) -> None:
                    if fd in _read_fds:
                        raise OSError("temporary read cleanup failure")
                    _real_close(fd)

                try:
                    with (
                        mock.patch(
                            "agent_team.recovery.os.open", side_effect=capture_open
                        ),
                        mock.patch(
                            "agent_team.recovery.os.read", side_effect=fail_read
                        ),
                        mock.patch(
                            "agent_team.recovery.os.close", side_effect=fail_close
                        ),
                        self.assertRaises(RecoveryLedgerError) as raised,
                    ):
                        if isinstance(log, RecoveryLedgerWriter):
                            log._read_ledger(root_fd)
                        else:
                            recovery_module._read_root_file(
                                root_fd,
                                RECOVERY_TOMBSTONES_BASENAME,
                                orphan_registry=log._orphan_fds,
                            )
                    self.assertIsNotNone(raised.exception.__cause__)
                    if isinstance(log, RecoveryLedgerWriter):
                        self.assertIn(
                            "recovery ledger read close status is unknown",
                            str(raised.exception.__cause__),
                        )
                    else:
                        self.assertIn(
                            "recovery read close status is unknown",
                            str(raised.exception.__cause__),
                        )
                    log.close()
                finally:
                    for fd in read_fds:
                        try:
                            real_close(fd)
                        except OSError:
                            pass
                    real_close(root_fd)

    def test_cleanup_retry_error_retains_same_owner_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-cleanup-retry-owner-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                marker = root / MARKER_NAME
                marker.write_bytes(b"malformed marker")
                real_open = os.open
                real_close = os.close
                real_flock = fcntl.flock
                gate_fds: list[int] = []

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _gate_fds: list[int] = gate_fds,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] == store_module.LIFETIME_GATE_FILENAME:
                        _gate_fds.append(fd)
                    return fd

                def fail_gate_close(
                    fd: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_close: Callable[[int], None] = real_close,
                ) -> None:
                    if fd in _gate_fds:
                        raise OSError("persistent gate close failure")
                    _real_close(fd)

                def fail_gate_unlock(
                    fd: int,
                    operation: int,
                    _gate_fds: list[int] = gate_fds,
                    _real_flock: Callable[[int, int], None] = real_flock,
                ) -> None:
                    if fd in _gate_fds and operation == fcntl.LOCK_UN:
                        raise OSError("persistent gate unlock failure")
                    _real_flock(fd, operation)

                log = log_factory(root, marker_name=MARKER_NAME)
                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.os.close", side_effect=fail_gate_close
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock",
                        side_effect=fail_gate_unlock,
                    ),
                ):
                    with self.assertRaises(RecoveryLedgerError) as raised:
                        log.read()
                    first_error = raised.exception
                    first_owner = first_error.cleanup_owner
                    self.assertIsNotNone(first_owner)
                    with self.assertRaises(doctor_module.DoctorError) as retried:
                        first_error.retry_cleanup()
                    retry_error = retried.exception
                    self.assertIs(first_owner, retry_error.cleanup_owner)
                    with self.assertRaises(doctor_module.DoctorError) as retried_again:
                        retry_error.retry_cleanup()
                    self.assertIs(first_owner, retried_again.exception.cleanup_owner)
                marker.write_bytes(store_module.WRITER_MARKER_CLEAN_CONTENT)
                retried_again.exception.retry_cleanup()
                retried_again.exception.retry_cleanup()
                self.assertIsNone(retried_again.exception.cleanup_owner)
                self.assertIsNone(log.read())

    def test_registry_retry_error_retains_same_owner_for_both_logs_and_kinds(
        self,
    ) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            for registry_name in ("_orphan_filesystems", "_orphan_controllers"):
                with (
                    self.subTest(log_type=log_factory.__name__, registry=registry_name),
                    tempfile.TemporaryDirectory(
                        prefix="agent-team-recovery-registry-retry-owner-"
                    ) as temporary,
                ):
                    root = _root(temporary)
                    with CoordinationStore(root):
                        pass
                    log = log_factory(root, marker_name=MARKER_NAME)
                    if registry_name == "_orphan_filesystems":
                        resource: StateFilesystem | WalSidecarController = (
                            StateFilesystem(
                                root,
                                marker_name=MARKER_NAME,
                                ledger_name=(
                                    RECOVERY_LEDGER_BASENAME
                                    if log_factory is RecoveryLedgerWriter
                                    else RECOVERY_TOMBSTONES_BASENAME
                                ),
                                busy_timeout_ms=0,
                            )
                        )
                    else:
                        resource = WalSidecarController(root, busy_timeout_ms=50)
                    registry = getattr(log, registry_name)
                    registry.append(resource)
                    with mock.patch.object(
                        type(resource),
                        "close",
                        side_effect=OSError("persistent registry cleanup failure"),
                    ):
                        with self.assertRaises(RecoveryLedgerError) as raised:
                            log.close()
                        first_error = raised.exception
                        first_owner = first_error.cleanup_owner
                        self.assertIsNotNone(first_owner)
                        with self.assertRaises(RecoveryLedgerError) as retried:
                            first_error.retry_cleanup()
                        self.assertIs(first_owner, retried.exception.cleanup_owner)
                        with self.assertRaises(RecoveryLedgerError) as retried_again:
                            retried.exception.retry_cleanup()
                        self.assertIs(
                            first_owner, retried_again.exception.cleanup_owner
                        )
                    retried_again.exception.retry_cleanup()
                    retried_again.exception.retry_cleanup()
                    self.assertIsNone(retried_again.exception.cleanup_owner)
                    self.assertEqual([], registry)

    def test_read_cleanup_chains_body_exception_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-read-body-cleanup-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                log = log_factory(root, marker_name=MARKER_NAME)
                if isinstance(log, RecoveryLedgerWriter):
                    first = self._record(1, "RESTORE_PREPARED")
                    log.initialize(first, self._initialization(first.backup_digest))
                body_error = RecoveryLedgerError("temporary read body failure")
                with mock.patch.object(
                    StateFilesystem,
                    "close",
                    side_effect=OSError("temporary read cleanup failure"),
                ):
                    if log_factory is RecoveryLedgerWriter:
                        read_patch = mock.patch.object(
                            log,
                            "_read_ledger",
                            side_effect=body_error,
                        )
                    else:
                        read_patch = mock.patch.object(
                            recovery_module,
                            "_read_root_file",
                            side_effect=body_error,
                        )
                    with read_patch, self.assertRaises(RecoveryLedgerError) as raised:
                        log.read()
                self.assertIs(body_error, raised.exception)
                self.assertIsNotNone(raised.exception.__cause__)
                self.assertIn(
                    "filesystem close status is unknown",
                    str(raised.exception.__cause__),
                )
                log.close()

    def test_recovery_orphan_registries_are_bounded_and_retain_overflow(self) -> None:
        max_resources = recovery_module._MAX_ORPHAN_RESOURCES
        filesystem_registry: list[StateFilesystem] = []
        filesystem_resources = [cast(StateFilesystem, mock.Mock()) for _ in range(32)]
        self._assert_bounded_registry(
            "filesystem",
            filesystem_registry,
            filesystem_resources,
            recovery_module._remember_orphan_filesystem,
            max_resources,
        )

        session_registry: list[QuiescenceSession] = []
        session_resources = [cast(QuiescenceSession, mock.Mock()) for _ in range(32)]
        self._assert_bounded_registry(
            "session",
            session_registry,
            session_resources,
            recovery_module._remember_orphan_session,
            max_resources,
        )

        controller_registry: list[WalSidecarController] = []
        controller_resources = [
            cast(WalSidecarController, mock.Mock()) for _ in range(32)
        ]
        self._assert_bounded_registry(
            "controller",
            controller_registry,
            controller_resources,
            recovery_module._remember_orphan_controller,
            max_resources,
        )

    def test_recovery_orphan_fd_overflow_owner_drains_existing_and_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-orphan-fd-overflow-"
        ) as temporary:
            root = _root(temporary)
            real_fstat = os.fstat
            real_close = os.close
            fds: list[int] = []
            registry: list[tuple[int, tuple[int, int] | None, str]] = []
            try:
                for index in range(recovery_module._MAX_ORPHAN_FDS + 1):
                    fd = os.open(
                        root / f"orphan-{index}",
                        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
                        0o600,
                    )
                    fds.append(fd)
                    metadata = real_fstat(fd)
                    entry = (
                        fd,
                        (metadata.st_dev, metadata.st_ino),
                        f"orphan {index}",
                    )
                    if index < recovery_module._MAX_ORPHAN_FDS:
                        registry.append(entry)
                overflow_fd = fds[-1]
                overflow_metadata = real_fstat(overflow_fd)
                owner = recovery_module._remember_orphan_fd(
                    registry,
                    None,
                    overflow_fd,
                    (overflow_metadata.st_dev, overflow_metadata.st_ino),
                    "orphan overflow",
                )
                self.assertIsNotNone(owner)
                assert owner is not None
                owner.retry_cleanup()
                owner.retry_cleanup()
                self.assertEqual([], registry)
                for fd in fds:
                    with self.assertRaises(OSError):
                        real_fstat(fd)
            finally:
                for fd in fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass

    def _assert_bounded_registry(
        self,
        registry_name: str,
        registry: list[_ResourceT],
        resources: list[_ResourceT],
        remember: Callable[[list[_ResourceT], _ResourceT], object],
        max_resources: int,
    ) -> None:
        with self.subTest(registry=registry_name):
            for resource in resources[:max_resources]:
                owner = remember(registry, resource)
                self.assertIsNotNone(owner)
            self.assertEqual(max_resources, len(registry))
            duplicate_owner = remember(registry, resources[0])
            self.assertIsNotNone(duplicate_owner)
            self.assertEqual(max_resources, len(registry))
            overflow = resources[max_resources]
            overflow_owner = remember(registry, overflow)
            self.assertIsNotNone(overflow_owner)
            self.assertEqual(max_resources, len(registry))
            assert overflow_owner is not None
            overflow_owner.retry_cleanup()  # type: ignore[attr-defined]
            cast(mock.Mock, overflow).close.assert_called_once()
            for existing in resources[:max_resources]:
                cast(mock.Mock, existing).close.assert_called_once()

    def test_retry_combines_all_failed_registry_owners_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-composite-retry-"
                ) as temporary,
            ):
                root = _root(temporary)
                log = log_factory(root, marker_name=MARKER_NAME)
                controller_mock = mock.Mock()
                session_mock = mock.Mock()
                controller = cast(WalSidecarController, controller_mock)
                session = cast(QuiescenceSession, session_mock)
                controller_mock.close.side_effect = OSError("controller retry failure")
                session_mock.close.side_effect = OSError("session retry failure")
                log._orphan_controllers.append(controller)
                log._orphan_sessions.append(session)
                with self.assertRaises(RecoveryLedgerError) as raised:
                    log.close()
                owner = raised.exception.cleanup_owner
                self.assertIsNotNone(owner)
                controller_mock.close.side_effect = None
                with self.assertRaises(RecoveryLedgerError) as retried:
                    raised.exception.retry_cleanup()
                self.assertIs(owner, retried.exception.cleanup_owner)
                self.assertEqual([], log._orphan_controllers)
                self.assertEqual([session], log._orphan_sessions)
                session_mock.close.side_effect = None
                retried.exception.retry_cleanup()
                retried.exception.retry_cleanup()
                self.assertIsNone(retried.exception.cleanup_owner)
                self.assertEqual([], log._orphan_sessions)

    def test_session_cleanup_error_carries_owner_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-session-owner-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                if log_factory is RecoveryLedgerWriter:
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(1, "RESTORE_PREPARED")
                    )
                    authority = self._initialization(record.backup_digest)
                else:
                    record = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                    )
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                log = log_factory(root, marker_name=MARKER_NAME)
                original_close = QuiescenceSession.close
                failed = False

                def fail_once(
                    session: QuiescenceSession,
                    _original_close: Callable[
                        [QuiescenceSession], None
                    ] = original_close,
                ) -> None:
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise OSError("session close body cleanup uncertainty")
                    _original_close(session)

                with (
                    mock.patch.object(QuiescenceSession, "close", new=fail_once),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    if isinstance(log, RecoveryLedgerWriter):
                        assert isinstance(record, RecoveryLedgerRecord)
                        log.initialize(record, authority)
                    else:
                        assert isinstance(record, RecoveryTombstoneRecord)
                        log.initialize(record, authority)
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                raised.exception.retry_cleanup()
                self.assertIsNone(raised.exception.cleanup_owner)
                self.assertEqual([], log._orphan_sessions)

    def test_attrless_body_gets_typed_cleanup_wrapper_for_both_logs(self) -> None:
        class BodySignal(BaseException):
            __slots__ = ()

        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-attrless-body-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                log = log_factory(root, marker_name=MARKER_NAME)
                ledger_name = (
                    RECOVERY_LEDGER_BASENAME
                    if log_factory is RecoveryLedgerWriter
                    else RECOVERY_TOMBSTONES_BASENAME
                )
                filesystem = StateFilesystem(
                    root,
                    marker_name=MARKER_NAME,
                    ledger_name=ledger_name,
                    busy_timeout_ms=0,
                )
                with (
                    mock.patch.object(
                        StateFilesystem,
                        "close",
                        side_effect=OSError("attrless cleanup failure"),
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                    log,
                ):
                    log._orphan_filesystems.append(filesystem)
                    raise BodySignal("attrless body")
                self.assertIsInstance(raised.exception.__cause__, BodySignal)
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                self.assertIsNone(raised.exception.cleanup_owner)

    def test_append_body_error_carries_cleanup_owner_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-append-body-owner-"
                ) as temporary,
            ):
                root = _root(temporary)
                if log_factory is RecoveryLedgerWriter:

                    class BodyWriter(RecoveryLedgerWriter):
                        def _fault(self, point: str) -> None:
                            if point == "before_final_check":
                                raise RecoveryLedgerError("append body failure")

                    log: RecoveryLedgerWriter | RecoveryTombstoneLog = BodyWriter(
                        root,
                        marker_name=MARKER_NAME,
                    )
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(
                            1,
                            "RESTORE_PREPARED",
                        )
                    )
                else:

                    class BodyTombstoneLog(RecoveryTombstoneLog):
                        def _fault(self, point: str) -> None:
                            if point == "before_final_check":
                                raise RecoveryLedgerError("append body failure")

                    log = BodyTombstoneLog(root, marker_name=MARKER_NAME)
                    record = RecoveryTombstoneRecord(
                        version=1,
                        sequence=1,
                        phase="PREPARED",
                        restore_generation=1,
                        backup_digest="sha256:" + "a" * 64,
                        previous_primary_digest="sha256:" + "b" * 64,
                        candidate_digest="sha256:" + "c" * 64,
                        previous_recovery_epoch=0,
                        previous_fencing_token_hwm=0,
                        previous_last_clock_ns=0,
                        identities=(),
                        actor="operator",
                        audit_ref="audit/1",
                    )
                with (
                    mock.patch.object(
                        StateFilesystem,
                        "close",
                        side_effect=OSError("append filesystem cleanup failure"),
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    if isinstance(log, RecoveryLedgerWriter):
                        assert isinstance(record, RecoveryLedgerRecord)
                        log._append_without_store(record, allow_create=True)
                    else:
                        assert isinstance(record, RecoveryTombstoneRecord)
                        log._append_without_store(record, allow_create=True)
                error = raised.exception
                self.assertIsNotNone(error.cleanup_owner)
                error.retry_cleanup()
                error.retry_cleanup()
                self.assertIsNone(error.cleanup_owner)
                self.assertEqual([], log._orphan_filesystems)

    def test_identity_unknown_handoff_is_retained_for_both_logs(self) -> None:
        for log_factory in (RecoveryLedgerWriter, RecoveryTombstoneLog):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-identity-unknown-"
                ) as temporary,
            ):
                root = _root(temporary)
                log = log_factory(root, marker_name=MARKER_NAME)
                target = root / "identity-unknown"
                real_open = os.open
                real_close = os.close
                real_fstat = os.fstat
                fd = real_open(target, os.O_CREAT | os.O_RDWR, 0o600)
                expected_identity = (
                    real_fstat(fd).st_dev,
                    real_fstat(fd).st_ino,
                )
                label = "identity-unknown read"

                def fail_fstat(_: int) -> os.stat_result:
                    raise OSError(errno.EIO, "descriptor status unavailable")

                with mock.patch("agent_team.recovery.os.fstat", side_effect=fail_fstat):
                    owner = recovery_module._remember_orphan_fd(
                        log._orphan_fds,
                        None,
                        fd,
                        expected_identity,
                        label,
                    )
                self.assertIsNotNone(owner)
                self.assertEqual([(fd, expected_identity, label)], log._orphan_fds)
                assert owner is not None
                owner.retry_cleanup()
                owner.retry_cleanup()
                self.assertEqual([], log._orphan_fds)
                with self.assertRaises(OSError):
                    real_fstat(fd)

                unresolved_fd = real_open(
                    root / "identity-unresolved",
                    os.O_CREAT | os.O_RDWR,
                    0o600,
                )
                unresolved_label = "identity-unresolved read"
                try:
                    with mock.patch(
                        "agent_team.recovery.os.fstat", side_effect=fail_fstat
                    ):
                        unresolved_owner = recovery_module._remember_orphan_fd(
                            log._orphan_fds,
                            None,
                            unresolved_fd,
                            None,
                            unresolved_label,
                        )
                    self.assertIsNotNone(unresolved_owner)
                    self.assertEqual(
                        [(unresolved_fd, None, unresolved_label)], log._orphan_fds
                    )
                    assert unresolved_owner is not None
                    unresolved_error = RecoveryLedgerError(
                        "unresolved descriptor requires cleanup"
                    )
                    unresolved_error._set_cleanup_owner(unresolved_owner)
                    with self.assertRaises(RecoveryLedgerError) as blocked:
                        unresolved_error.retry_cleanup()
                    self.assertIs(
                        unresolved_owner,
                        blocked.exception.cleanup_owner,
                    )
                    real_fstat(unresolved_fd)
                    real_close(unresolved_fd)
                    blocked.exception.retry_cleanup()
                    self.assertEqual([], log._orphan_fds)
                finally:
                    try:
                        real_close(unresolved_fd)
                    except OSError:
                        pass

    def test_orphan_fd_retry_wraps_arbitrary_inner_status(self) -> None:
        class BodySignal(BaseException):
            __slots__ = ()

        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-orphan-inner-status-"
        ) as temporary:
            root = _root(temporary)
            target = root / "orphan-inner-status"
            real_open = os.open
            real_close = os.close
            real_fstat = os.fstat
            fd = real_open(target, os.O_CREAT | os.O_RDWR, 0o600)
            identity = (
                real_fstat(fd).st_dev,
                real_fstat(fd).st_ino,
            )
            registry: list[tuple[int, tuple[int, int] | None, str]] = [
                (fd, identity, "orphan inner status")
            ]
            fstat_calls = 0

            def fail_fstat(
                candidate: int,
                _real_fstat: Callable[[int], os.stat_result] = real_fstat,
            ) -> os.stat_result:
                nonlocal fstat_calls
                if candidate == fd:
                    fstat_calls += 1
                    if fstat_calls == 2:
                        raise BodySignal("inner status probe")
                return _real_fstat(candidate)

            def fail_close(
                candidate: int,
                _real_close: Callable[[int], None] = real_close,
            ) -> None:
                if candidate == fd:
                    raise OSError("first close response loss")
                _real_close(candidate)

            try:
                with (
                    mock.patch(
                        "agent_team.recovery.os.fstat",
                        side_effect=fail_fstat,
                    ),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=fail_close,
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    recovery_module._retry_orphan_fds(registry)
                self.assertIsNotNone(raised.exception.cleanup_owner)
                self.assertEqual(1, len(registry))
                raised.exception.retry_cleanup()
                self.assertEqual([], registry)
                raised.exception.retry_cleanup()
            finally:
                try:
                    real_close(fd)
                except OSError:
                    pass

    def test_custom_read_retention_callback_keeps_body_behavior(self) -> None:
        class BodySignal(BaseException):
            __slots__ = ()

        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-owner-read-cleanup-"
        ) as temporary:
            root = _root(temporary)
            target = root / RECOVERY_TOMBSTONES_BASENAME
            target.write_bytes(b"body")
            target.chmod(0o600)
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            real_open = os.open
            real_read = os.read
            real_close = os.close
            read_fds: list[int] = []
            retained: list[tuple[int, tuple[int, int] | None, str]] = []

            def capture_open(
                *args: object,
                _real_open: Callable[..., int] = real_open,
                _read_fds: list[int] = read_fds,
                **kwargs: object,
            ) -> int:
                fd = _real_open(*args, **kwargs)
                if args and args[0] == RECOVERY_TOMBSTONES_BASENAME:
                    _read_fds.append(fd)
                return fd

            def fail_read(
                fd: int,
                size: int,
                _real_read: Callable[[int, int], bytes] = real_read,
                _read_fds: list[int] = read_fds,
            ) -> bytes:
                if fd in _read_fds:
                    raise BodySignal("owner read body failure")
                return _real_read(fd, size)

            def fail_close(
                fd: int,
                _read_fds: list[int] = read_fds,
                _real_close: Callable[[int], None] = real_close,
            ) -> None:
                if fd in _read_fds:
                    raise OSError("owner read close response loss")
                _real_close(fd)

            def retain(
                fd: int,
                expected_identity: tuple[int, int] | None,
                label: str,
                _retained: list[tuple[int, tuple[int, int] | None, str]] = retained,
            ) -> None:
                _retained.append((fd, expected_identity, label))

            try:
                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch("agent_team.recovery.os.read", side_effect=fail_read),
                    mock.patch("agent_team.recovery.os.close", side_effect=fail_close),
                    self.assertRaises(BodySignal),
                ):
                    recovery_module._read_root_file(
                        root_fd,
                        RECOVERY_TOMBSTONES_BASENAME,
                        retain_fd=retain,
                    )
                for fd, _, _ in retained:
                    real_close(fd)
            finally:
                for fd, _, _ in retained:
                    try:
                        real_close(fd)
                    except OSError:
                        pass
                try:
                    os.close(root_fd)
                except OSError:
                    pass

    def test_tombstone_read_owned_body_and_close_failure_is_retryable(self) -> None:
        class BodySignal(BaseException):
            __slots__ = ()

        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-tombstone-owned-read-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            record = RecoveryTombstoneTest._record(1, "PREPARED")
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            tombstone_path.write_bytes(_encode_tombstone(record))
            tombstone_path.chmod(0o600)
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            owner = session.issue_owner()
            log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            real_open = os.open
            real_read = os.read
            real_close = os.close
            read_fds: list[int] = []

            def capture_open(
                *args: object,
                _real_open: Callable[..., int] = real_open,
                _read_fds: list[int] = read_fds,
                **kwargs: object,
            ) -> int:
                fd = _real_open(*args, **kwargs)
                if args and args[0] == RECOVERY_TOMBSTONES_BASENAME:
                    _read_fds.append(fd)
                return fd

            def fail_read(
                fd: int,
                size: int,
                _real_read: Callable[[int, int], bytes] = real_read,
                _read_fds: list[int] = read_fds,
            ) -> bytes:
                if fd in _read_fds:
                    raise BodySignal("owned tombstone read body failure")
                return _real_read(fd, size)

            def fail_close(
                fd: int,
                _read_fds: list[int] = read_fds,
                _real_close: Callable[[int], None] = real_close,
            ) -> None:
                if fd in _read_fds:
                    raise OSError("owned tombstone read close failure")
                _real_close(fd)

            try:
                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch("agent_team.recovery.os.read", side_effect=fail_read),
                    mock.patch("agent_team.recovery.os.close", side_effect=fail_close),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    log.read_owned(owner)
                self.assertIsInstance(raised.exception.__cause__, BodySignal)
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                raised.exception.retry_cleanup()
                self.assertEqual([], log._orphan_fds)
            finally:
                session.close()

    def test_pair_unlock_identity_reuse_does_not_close_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-pair-unlock-reuse-"
        ) as temporary:
            root = _root(temporary)
            ledger = self._record(1, "RESTORE_PREPARED")
            tombstone = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref=ledger.audit_ref,
            )
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            ledger_path.write_bytes(_encode_record(ledger))
            ledger_path.chmod(0o600)
            tombstone_path.write_bytes(_encode_tombstone(tombstone))
            tombstone_path.chmod(0o600)
            owner = BorrowedRootOwner(root)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            real_open = os.open
            real_close = os.close
            real_flock = fcntl.flock
            fd_names: dict[int, str] = {}
            foreign_fd: int | None = None
            foreign_path = root / "foreign-unlock-target"

            def capture_open(
                *args: object,
                _real_open: Callable[..., int] = real_open,
                _fd_names: dict[int, str] = fd_names,
                **kwargs: object,
            ) -> int:
                fd = _real_open(*args, **kwargs)
                if args and args[0] in {
                    RECOVERY_LEDGER_BASENAME,
                    RECOVERY_TOMBSTONES_BASENAME,
                }:
                    _fd_names[fd] = str(args[0])
                return fd

            def swap_on_unlock(
                fd: int,
                operation: int,
                _real_flock: Callable[[int, int], None] = real_flock,
            ) -> None:
                nonlocal foreign_fd
                if (
                    operation == fcntl.LOCK_UN
                    and fd_names.get(fd) == RECOVERY_TOMBSTONES_BASENAME
                    and foreign_fd is None
                ):
                    real_close(fd)
                    foreign_path.write_bytes(b"foreign")
                    foreign_path.chmod(0o600)
                    candidate_fd = real_open(
                        foreign_path,
                        os.O_RDONLY | os.O_CLOEXEC,
                    )
                    if candidate_fd != fd:
                        os.dup2(candidate_fd, fd)
                        real_close(candidate_fd)
                    foreign_fd = fd
                    return
                _real_flock(fd, operation)

            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner",
                        BorrowedRootOwner,
                    ),
                    mock.patch(
                        "agent_team.recovery.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock",
                        side_effect=swap_on_unlock,
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    restore_ledger.read_for_resume(owner)
                self.assertIsNotNone(foreign_fd)
                assert foreign_fd is not None
                real_fstat = os.fstat
                real_fstat(foreign_fd)
            finally:
                if foreign_fd is not None:
                    try:
                        real_close(foreign_fd)
                    except OSError:
                        pass
                owner.close()

    def test_append_unlock_identity_reuse_does_not_close_foreign_fd(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-append-unlock-reuse-"
        ) as temporary:
            root = _root(temporary)
            owner = BorrowedRootOwner(root)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            second = self._record(2, "RESTORE_REPLACED")
            with mock.patch(
                "agent_team.recovery.QuiescenceOwner",
                BorrowedRootOwner,
            ):
                writer.initialize_owned(
                    first,
                    self._initialization(first.backup_digest),
                    owner,
                )
            real_open = os.open
            real_close = os.close
            real_flock = fcntl.flock
            write_fds: list[int] = []
            foreign_fd: int | None = None
            foreign_path = root / "foreign-append-unlock-target"

            def capture_open(
                *args: object,
                _real_open: Callable[..., int] = real_open,
                _write_fds: list[int] = write_fds,
                **kwargs: object,
            ) -> int:
                fd = _real_open(*args, **kwargs)
                flags = args[1] if len(args) > 1 else None
                if type(flags) is int and flags & os.O_WRONLY:
                    _write_fds.append(fd)
                return fd

            def swap_on_unlock(
                fd: int,
                operation: int,
                _real_flock: Callable[[int, int], None] = real_flock,
            ) -> None:
                nonlocal foreign_fd
                if (
                    operation == fcntl.LOCK_UN
                    and fd in write_fds
                    and foreign_fd is None
                ):
                    real_close(fd)
                    foreign_path.write_bytes(b"foreign")
                    foreign_path.chmod(0o600)
                    candidate_fd = real_open(
                        foreign_path,
                        os.O_RDONLY | os.O_CLOEXEC,
                    )
                    if candidate_fd != fd:
                        os.dup2(candidate_fd, fd)
                        real_close(candidate_fd)
                    foreign_fd = fd
                    return
                _real_flock(fd, operation)

            try:
                with self.assertRaises(RecoveryLedgerError):
                    stack = ExitStack()
                    try:
                        stack.enter_context(
                            mock.patch(
                                "agent_team.recovery.os.open",
                                side_effect=capture_open,
                            )
                        )
                        stack.enter_context(
                            mock.patch(
                                "agent_team.recovery.fcntl.flock",
                                side_effect=swap_on_unlock,
                            )
                        )
                        stack.enter_context(
                            mock.patch(
                                "agent_team.recovery.QuiescenceOwner",
                                BorrowedRootOwner,
                            )
                        )
                        writer.append_owned(second, owner)
                    finally:
                        stack.close()
                self.assertIsNotNone(foreign_fd)
                assert foreign_fd is not None
                os.fstat(foreign_fd)
            finally:
                if foreign_fd is not None:
                    try:
                        real_close(foreign_fd)
                    except OSError:
                        pass
                owner.close()

    def test_pair_close_failures_retain_both_fds_in_error_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-pair-close-failures-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger = self._record(1, "RESTORE_PREPARED")
            tombstone = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref=ledger.audit_ref,
            )
            _write_recovery_pair(
                root,
                (ledger,),
                (tombstone,),
            )
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            owner = session.issue_owner()
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            real_open = os.open
            real_close = os.close
            opened_fds: list[int] = []

            def capture_open(
                *args: object,
                _real_open: Callable[..., int] = real_open,
                _opened_fds: list[int] = opened_fds,
                **kwargs: object,
            ) -> int:
                fd = _real_open(*args, **kwargs)
                if args and args[0] in {
                    RECOVERY_LEDGER_BASENAME,
                    RECOVERY_TOMBSTONES_BASENAME,
                }:
                    _opened_fds.append(fd)
                return fd

            def fail_close(
                fd: int,
                _opened_fds: list[int] = opened_fds,
                _real_close: Callable[[int], None] = real_close,
            ) -> None:
                if fd in _opened_fds:
                    raise OSError("both recovery close responses lost")
                _real_close(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.recovery.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=fail_close,
                    ),
                    self.assertRaises(RecoveryLedgerError) as raised,
                ):
                    restore_ledger.read_for_resume(owner)
                self.assertEqual(2, len(opened_fds))
                self.assertIsNotNone(raised.exception.cleanup_owner)
                raised.exception.retry_cleanup()
                raised.exception.retry_cleanup()
                for fd in opened_fds:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
            finally:
                for fd in opened_fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass
                session.close()

    def test_pair_handoff_rejection_keeps_second_fd_in_fallback_owner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-pair-handoff-rejection-"
        ) as temporary:
            root = _root(temporary)
            ledger = self._record(1, "RESTORE_PREPARED")
            tombstone = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref=ledger.audit_ref,
            )
            _write_recovery_pair(root, (ledger,), (tombstone,))
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            real_open = os.open
            real_close = os.close
            opened_fds: list[int] = []
            orphan_registry: list[tuple[int, tuple[int, int] | None, str]] = []

            def fail_close(
                fd: int,
                _opened_fds: list[int] = opened_fds,
                _real_close: Callable[[int], None] = real_close,
            ) -> None:
                if fd in _opened_fds:
                    raise OSError("pair close response loss")
                _real_close(fd)

            def reject_handoff(
                fd: int,
                _expected_identity: tuple[int, int] | None,
                _label: str,
            ) -> None:
                del fd
                raise OSError("owner rejected descriptor")

            def capture_open(
                *args: object,
                _real_open: Callable[..., int] = real_open,
                _opened_fds: list[int] = opened_fds,
                **kwargs: object,
            ) -> int:
                fd = _real_open(*args, **kwargs)
                if args and args[0] in {
                    RECOVERY_LEDGER_BASENAME,
                    RECOVERY_TOMBSTONES_BASENAME,
                }:
                    _opened_fds.append(fd)
                return fd

            try:
                with self.assertRaises(RecoveryLedgerError) as raised:
                    stack = ExitStack()
                    try:
                        stack.enter_context(
                            mock.patch(
                                "agent_team.recovery.os.open",
                                side_effect=capture_open,
                            )
                        )
                        stack.enter_context(
                            mock.patch(
                                "agent_team.recovery.os.close",
                                side_effect=fail_close,
                            )
                        )
                        files = stack.enter_context(
                            recovery_module._locked_restore_files(
                                root_fd,
                                reject_handoff,
                                orphan_registry,
                            )
                        )
                        opened_fds[:] = [current.fd for current in files]
                    finally:
                        stack.close()
                self.assertIsNotNone(raised.exception.cleanup_owner)
                self.assertEqual(2, len(orphan_registry))
                raised.exception.retry_cleanup()
                raised.exception.retry_cleanup()
                self.assertEqual([], orphan_registry)
                for fd in opened_fds:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
            finally:
                for fd in opened_fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass
                real_close(root_fd)

    def test_ledger_first_generation_is_exactly_one_at_every_entrypoint(self) -> None:
        for generation in (0, 2):
            with self.subTest(generation=generation):
                valid = self._record(1, "RESTORE_PREPARED")
                if generation == 0:
                    with self.assertRaises(ValueError):
                        replace(valid, restore_generation=generation)
                else:
                    replace(valid, restore_generation=generation)
                record = object.__new__(RecoveryLedgerRecord)
                for name in (
                    "version",
                    "sequence",
                    "phase",
                    "restore_generation",
                    "recovery_epoch",
                    "fencing_token_floor",
                    "backup_digest",
                    "actor",
                    "audit_ref",
                ):
                    object.__setattr__(
                        record, name, object.__getattribute__(valid, name)
                    )
                object.__setattr__(record, "restore_generation", generation)
                raw = _encode_record(record)
                with self.assertRaises((RecoveryLedgerError, LedgerReadError)):
                    RecoveryLedgerWriter._latest_from_bytes(raw, allow_empty=False)
                with tempfile.TemporaryDirectory(
                    prefix="agent-team-ledger-first-generation-"
                ) as temporary:
                    root = _root(temporary)
                    ledger_path = root / RECOVERY_LEDGER_BASENAME
                    writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
                    with self.assertRaises(RecoveryLedgerError):
                        writer.initialize(
                            record,
                            self._initialization(record.backup_digest),
                        )
                    ledger_path.write_bytes(raw)
                    ledger_path.chmod(0o600)
                    with self.assertRaises(RecoveryLedgerError):
                        writer.read()

    def test_tombstone_first_generation_is_exactly_one_at_every_entrypoint(
        self,
    ) -> None:
        for generation in (2,):
            with self.subTest(generation=generation):
                valid = RecoveryTombstoneRecord(
                    version=1,
                    sequence=1,
                    phase="PREPARED",
                    restore_generation=1,
                    backup_digest="sha256:" + "a" * 64,
                    previous_primary_digest="sha256:" + "b" * 64,
                    candidate_digest="sha256:" + "c" * 64,
                    previous_recovery_epoch=0,
                    previous_fencing_token_hwm=0,
                    previous_last_clock_ns=0,
                    identities=(),
                    actor="operator",
                    audit_ref="audit/1",
                )
                replace(valid, restore_generation=generation)
                record = object.__new__(RecoveryTombstoneRecord)
                for name in (
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
                ):
                    object.__setattr__(
                        record, name, object.__getattribute__(valid, name)
                    )
                object.__setattr__(record, "restore_generation", generation)
                raw = _encode_tombstone(record)
                with tempfile.TemporaryDirectory(
                    prefix="agent-team-tombstone-first-generation-"
                ) as temporary:
                    root = _root(temporary)
                    tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                    log = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                    with self.assertRaises(RecoveryLedgerError):
                        log.initialize(record, authority)
                    tombstone_path.write_bytes(raw)
                    tombstone_path.chmod(0o600)
                    with self.assertRaises(RecoveryLedgerError):
                        recovery_module._latest_tombstone(raw, allow_empty=False)
                    with self.assertRaises(RecoveryLedgerError):
                        log.read()

    def test_durability_barrier_tombstone_first_initial_and_prepared_pair(
        self,
    ) -> None:
        for log_factory, record_kind in (
            (RecoveryLedgerWriter, "ledger"),
            (RecoveryTombstoneLog, "tombstone"),
        ):
            with (
                self.subTest(log_type=log_factory.__name__),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-durability-barrier-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                if record_kind == "ledger":
                    record: RecoveryLedgerRecord | RecoveryTombstoneRecord = (
                        self._record(
                            1,
                            "RESTORE_PREPARED",
                        )
                    )
                    authority = self._initialization(record.backup_digest)
                else:
                    record = RecoveryTombstoneTest._record(1, "PREPARED")
                    authority = _issue_recovery_ledger_initialization(
                        operator_id=record.actor,
                        audit_ref=record.audit_ref,
                        request_digest=record.backup_digest,
                    )
                log = log_factory(root, marker_name=MARKER_NAME)
                controller = WalSidecarController(root)
                session = controller.hold_quiescence()
                try:
                    owner = session.issue_owner()
                    if isinstance(log, RecoveryLedgerWriter):
                        assert isinstance(record, RecoveryLedgerRecord)
                        log.initialize_owned(record, authority, owner)
                    else:
                        assert isinstance(record, RecoveryTombstoneRecord)
                        log.initialize_owned(record, owner)
                    path = root / (
                        RECOVERY_LEDGER_BASENAME
                        if record_kind == "ledger"
                        else RECOVERY_TOMBSTONES_BASENAME
                    )
                    before = path.read_bytes()
                    real_fsync = os.fsync
                    failed = False

                    def fail_once(
                        fd: int,
                        _real_fsync: Callable[[int], None] = real_fsync,
                    ) -> None:
                        nonlocal failed
                        if not failed:
                            failed = True
                            raise OSError("durability barrier fsync failure")
                        _real_fsync(fd)

                    with (
                        mock.patch(
                            "agent_team.recovery.os.fsync", side_effect=fail_once
                        ),
                        self.assertRaises(RecoveryDurabilityError),
                    ):
                        log.ensure_durable_owned(owner)
                    self.assertEqual(before, path.read_bytes())
                    self.assertEqual(record, log.ensure_durable_owned(owner))
                    self.assertEqual(record, log.ensure_durable_owned(owner))
                finally:
                    session.close()

    def test_durability_barrier_tombstone_first_next_generation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-durability-next-generation-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            tombstones = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            ledger_records = (
                self._record(1, "RESTORE_PREPARED"),
                self._record(2, "RESTORE_REPLACED"),
                self._record(3, "RESTORE_COMMITTED"),
            )
            tombstone_records = (
                RecoveryTombstoneTest._record(1, "PREPARED"),
                RecoveryTombstoneTest._record(2, "COMMITTED"),
                RecoveryTombstoneTest._record(3, "PREPARED", generation=2),
            )
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            try:
                owner = session.issue_owner()
                ledger.initialize_owned(
                    ledger_records[0],
                    self._initialization(ledger_records[0].backup_digest),
                    owner,
                )
                for ledger_record in ledger_records[1:]:
                    ledger.append_owned(ledger_record, owner)
                tombstones.initialize_owned(tombstone_records[0], owner)
                for tombstone_record in tombstone_records[1:]:
                    tombstones.append_owned(tombstone_record, owner)
                path = root / RECOVERY_TOMBSTONES_BASENAME
                before = path.read_bytes()

                def fail_fsync(_: int) -> None:
                    raise OSError("next-generation durability fsync failure")

                with (
                    mock.patch("agent_team.recovery.os.fsync", side_effect=fail_fsync),
                    self.assertRaises(RecoveryDurabilityError),
                ):
                    tombstones.ensure_durable_owned(owner)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(
                    tombstone_records[-1],
                    tombstones.ensure_durable_owned(owner),
                )
                self.assertEqual(
                    tombstone_records[-1],
                    tombstones.ensure_durable_owned(owner),
                )
            finally:
                session.close()

    def test_read_for_resume_redurabilizes_pair_in_tombstone_first_order(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-pair-durability-order-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            tombstones = RecoveryTombstoneLog(root, marker_name=MARKER_NAME)
            ledger_record = self._record(1, "RESTORE_PREPARED")
            tombstone_record = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref=ledger_record.audit_ref,
            )
            authority = self._initialization(ledger_record.backup_digest)
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            try:
                owner = session.issue_owner()
                ledger.initialize_owned(ledger_record, authority, owner)
                tombstones.initialize_owned(tombstone_record, owner)
                restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                real_open = os.open
                real_flock = fcntl.flock
                fd_names: dict[int, str] = {}
                lock_names: list[str] = []
                fsync_fds: list[int] = []

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _fd_names: dict[int, str] = fd_names,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] in {
                        RECOVERY_LEDGER_BASENAME,
                        RECOVERY_TOMBSTONES_BASENAME,
                    }:
                        _fd_names[fd] = str(args[0])
                    return fd

                def trace_flock(
                    fd: int,
                    operation: int,
                    _real_flock: Callable[[int, int], None] = real_flock,
                    _fd_names: dict[int, str] = fd_names,
                    _lock_names: list[str] = lock_names,
                ) -> None:
                    if operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                        _lock_names.append(_fd_names.get(fd, "unknown"))
                    _real_flock(fd, operation)

                def trace_fsync(
                    fd: int,
                    _real_fsync: Callable[[int], None] = os.fsync,
                    _fsync_fds: list[int] = fsync_fds,
                ) -> None:
                    _fsync_fds.append(fd)
                    _real_fsync(fd)

                with (
                    mock.patch("agent_team.recovery.os.open", side_effect=capture_open),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock", side_effect=trace_flock
                    ),
                    mock.patch("agent_team.recovery.os.fsync", side_effect=trace_fsync),
                ):
                    state = restore_ledger.read_for_resume(owner)
                self.assertIsInstance(state, RestoreHandle)
                self.assertEqual(
                    [RECOVERY_TOMBSTONES_BASENAME, RECOVERY_LEDGER_BASENAME],
                    lock_names,
                )
                self.assertGreaterEqual(len(fsync_fds), 3)
            finally:
                session.close()

    def test_read_for_resume_redurabilizes_all_allowed_phase_pairs(self) -> None:
        phase_pairs = {
            "P/P": (("RESTORE_PREPARED",), ("PREPARED",)),
            "R/P": (("RESTORE_PREPARED", "RESTORE_REPLACED"), ("PREPARED",)),
            "R/C": (
                ("RESTORE_PREPARED", "RESTORE_REPLACED"),
                ("PREPARED", "COMMITTED"),
            ),
            "P/A": (("RESTORE_PREPARED",), ("PREPARED", "ABORTED")),
            "C/C": (
                ("RESTORE_PREPARED", "RESTORE_REPLACED", "RESTORE_COMMITTED"),
                ("PREPARED", "COMMITTED"),
            ),
            "A/A": (("RESTORE_PREPARED", "RESTORE_ABORTED"), ("PREPARED", "ABORTED")),
        }
        for name, (ledger_phases, tombstone_phases) in phase_pairs.items():
            with (
                self.subTest(pair=name),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-allowed-pair-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                ledger_records = tuple(
                    self._record(sequence, phase)
                    for sequence, phase in enumerate(ledger_phases, start=1)
                )
                tombstone_records = tuple(
                    replace(
                        RecoveryTombstoneTest._record(
                            sequence,
                            cast(TombstonePhase, phase),
                        ),
                        audit_ref=ledger_records[0].audit_ref,
                    )
                    for sequence, phase in enumerate(tombstone_phases, start=1)
                )
                (root / RECOVERY_LEDGER_BASENAME).write_bytes(
                    b"".join(_encode_record(record) for record in ledger_records)
                )
                (root / RECOVERY_LEDGER_BASENAME).chmod(0o600)
                (root / RECOVERY_TOMBSTONES_BASENAME).write_bytes(
                    b"".join(_encode_tombstone(record) for record in tombstone_records)
                )
                (root / RECOVERY_TOMBSTONES_BASENAME).chmod(0o600)
                controller = WalSidecarController(root)
                session = controller.hold_quiescence()
                try:
                    state = RestoreLedger(
                        root, marker_name=MARKER_NAME
                    ).read_for_resume(session.issue_owner())
                    self.assertIsInstance(state, RestoreHandle)
                finally:
                    session.close()

    def test_read_for_resume_rejects_malformed_or_mixed_pair_before_fsync(self) -> None:
        for tombstone_bytes in (
            b'{"version":1,\n',
            _encode_tombstone(
                replace(
                    RecoveryTombstoneTest._record(1, "PREPARED"),
                    actor="different-actor",
                )
            ),
        ):
            with (
                self.subTest(tombstone_bytes=tombstone_bytes),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-recovery-pair-malformed-"
                ) as temporary,
            ):
                root = _root(temporary)
                with CoordinationStore(root):
                    pass
                ledger = self._record(1, "RESTORE_PREPARED")
                ledger_path = root / RECOVERY_LEDGER_BASENAME
                tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                ledger_path.write_bytes(_encode_record(ledger))
                ledger_path.chmod(0o600)
                tombstone_path.write_bytes(tombstone_bytes)
                tombstone_path.chmod(0o600)
                before = (ledger_path.read_bytes(), tombstone_path.read_bytes())
                controller = WalSidecarController(root)
                session = controller.hold_quiescence()
                try:
                    owner = session.issue_owner()
                    fsync_calls: list[int] = []

                    def trace_fsync(
                        fd: int,
                        _fsync_calls: list[int] = fsync_calls,
                    ) -> None:
                        _fsync_calls.append(fd)

                    with (
                        mock.patch(
                            "agent_team.recovery.os.fsync", side_effect=trace_fsync
                        ),
                        self.assertRaises(RecoveryLedgerError),
                    ):
                        RestoreLedger(root, marker_name=MARKER_NAME).read_for_resume(
                            owner
                        )
                    self.assertEqual([], fsync_calls)
                    self.assertEqual(
                        before,
                        (ledger_path.read_bytes(), tombstone_path.read_bytes()),
                    )
                finally:
                    session.close()

    def test_read_for_resume_requires_owner_and_rejects_busy_tombstone(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-pair-owner-busy-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger = self._record(1, "RESTORE_PREPARED")
            tombstone = RecoveryTombstoneTest._record(1, "PREPARED")
            tombstone = replace(tombstone, audit_ref=ledger.audit_ref)
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            ledger_path.write_bytes(_encode_record(ledger))
            ledger_path.chmod(0o600)
            tombstone_path.write_bytes(_encode_tombstone(tombstone))
            tombstone_path.chmod(0o600)
            restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
            fsync_calls: list[int] = []

            def trace_fsync(fd: int, _fsync_calls: list[int] = fsync_calls) -> None:
                _fsync_calls.append(fd)

            with (
                mock.patch("agent_team.recovery.os.fsync", side_effect=trace_fsync),
                self.assertRaises(RecoveryLedgerError),
            ):
                restore_ledger.read_for_resume(object())
            self.assertEqual([], fsync_calls)

            held_fd = os.open(tombstone_path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                controller = WalSidecarController(root)
                session = controller.hold_quiescence()
                try:
                    with (
                        mock.patch(
                            "agent_team.recovery.os.fsync", side_effect=trace_fsync
                        ),
                        self.assertRaises(RecoveryLedgerError),
                    ):
                        restore_ledger.read_for_resume(session.issue_owner())
                    self.assertEqual([], fsync_calls)
                finally:
                    session.close()
            finally:
                os.close(held_fd)

    def test_read_for_resume_rejects_file_identity_swap_after_fsync(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-pair-identity-swap-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            ledger_path.write_bytes(_encode_record(self._record(1, "RESTORE_PREPARED")))
            ledger_path.chmod(0o600)
            tombstone = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref=self._record(1, "RESTORE_PREPARED").audit_ref,
            )
            tombstone_path.write_bytes(_encode_tombstone(tombstone))
            tombstone_path.chmod(0o600)
            before = (ledger_path.read_bytes(), tombstone_path.read_bytes())
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            try:
                owner = session.issue_owner()
                real_fsync = os.fsync
                swapped = False

                def swap_after_first_fsync(fd: int) -> None:
                    nonlocal swapped
                    if not swapped:
                        swapped = True
                        replacement = tombstone_path.with_name("tombstone-replacement")
                        tombstone_path.rename(replacement)
                        tombstone_path.write_bytes(before[1])
                        tombstone_path.chmod(0o600)
                    real_fsync(fd)

                with (
                    mock.patch(
                        "agent_team.recovery.os.fsync",
                        side_effect=swap_after_first_fsync,
                    ),
                    self.assertRaises(RecoveryDurabilityError),
                ):
                    RestoreLedger(root, marker_name=MARKER_NAME).read_for_resume(owner)
                self.assertEqual(before[0], ledger_path.read_bytes())
                self.assertEqual(before[1], tombstone_path.read_bytes())
            finally:
                session.close()

    def test_read_for_resume_redurabilizes_tombstone_first_response_loss(self) -> None:
        cases = ("initial", "next")
        for case in cases:
            failure_calls = (1, 2) if case == "initial" else (1, 3)
            for failure_call in failure_calls:
                with (
                    self.subTest(case=case, failure_call=failure_call),
                    tempfile.TemporaryDirectory(
                        prefix="agent-team-recovery-tombstone-first-loss-"
                    ) as temporary,
                ):
                    root = _root(temporary)
                    with CoordinationStore(root):
                        pass
                    ledger_path = root / RECOVERY_LEDGER_BASENAME
                    tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
                    if case == "initial":
                        ledger_before = None
                        tombstone = replace(
                            RecoveryTombstoneTest._record(1, "PREPARED"),
                            audit_ref=self._record(
                                1,
                                "RESTORE_PREPARED",
                            ).audit_ref,
                        )
                        tombstone_before = _encode_tombstone(tombstone)
                    else:
                        ledger_records = (
                            self._record(1, "RESTORE_PREPARED"),
                            self._record(2, "RESTORE_REPLACED"),
                            self._record(3, "RESTORE_COMMITTED"),
                        )
                        ledger_before = b"".join(
                            _encode_record(record) for record in ledger_records
                        )
                        tombstone_records = (
                            replace(
                                RecoveryTombstoneTest._record(1, "PREPARED"),
                                audit_ref="audit/1",
                            ),
                            replace(
                                RecoveryTombstoneTest._record(2, "COMMITTED"),
                                audit_ref="audit/1",
                            ),
                            replace(
                                RecoveryTombstoneTest._record(
                                    3,
                                    "PREPARED",
                                    generation=2,
                                ),
                                audit_ref="audit/2",
                            ),
                        )
                        tombstone_before = b"".join(
                            _encode_tombstone(record) for record in tombstone_records
                        )
                    if ledger_before is not None:
                        ledger_path.write_bytes(ledger_before)
                        ledger_path.chmod(0o600)
                    tombstone_path.write_bytes(tombstone_before)
                    tombstone_path.chmod(0o600)
                    controller = WalSidecarController(root)
                    session = controller.hold_quiescence()
                    try:
                        owner = session.issue_owner()
                        real_fsync = os.fsync
                        fsync_calls = 0

                        def fail_once(
                            fd: int,
                            _failure_call: int = failure_call,
                            _real_fsync: Callable[[int], None] = real_fsync,
                        ) -> None:
                            nonlocal fsync_calls
                            fsync_calls += 1
                            if fsync_calls == _failure_call:
                                raise OSError("simulated response loss")
                            _real_fsync(fd)

                        restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                        with (
                            mock.patch(
                                "agent_team.recovery.os.fsync",
                                side_effect=fail_once,
                            ),
                            self.assertRaises(RecoveryDurabilityError) as raised,
                        ):
                            restore_ledger.read_for_resume(owner)
                        self.assertEqual(failure_call, fsync_calls)
                        self.assertEqual(tombstone_before, tombstone_path.read_bytes())
                        if ledger_before is None:
                            self.assertFalse(ledger_path.exists())
                        else:
                            self.assertEqual(ledger_before, ledger_path.read_bytes())
                        raised.exception.retry_cleanup()
                        self.assertEqual(failure_call, fsync_calls)
                        state = restore_ledger.read_for_resume(owner)
                        self.assertIsInstance(state, RestoreTombstoneOrphan)
                        assert isinstance(state, RestoreTombstoneOrphan)
                        self.assertEqual(
                            "TOMBSTONE_FIRST_INITIAL"
                            if case == "initial"
                            else "TOMBSTONE_FIRST_NEXT",
                            state.kind,
                        )
                    finally:
                        session.close()

    def test_complete_tombstone_first_rechecks_durable_pair_before_append(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-recovery-complete-tombstone-first-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            tombstone = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref="audit/1",
            )
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            tombstone_bytes = _encode_tombstone(tombstone)
            tombstone_path.write_bytes(tombstone_bytes)
            tombstone_path.chmod(0o600)
            controller = WalSidecarController(root)
            session = controller.hold_quiescence()
            try:
                owner = session.issue_owner()
                restore_ledger = RestoreLedger(root, marker_name=MARKER_NAME)
                orphan = restore_ledger.read_for_resume(owner)
                self.assertIsInstance(orphan, RestoreTombstoneOrphan)
                assert isinstance(orphan, RestoreTombstoneOrphan)
                real_fsync = os.fsync
                failed = False

                def fail_once(fd: int) -> None:
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise OSError("simulated resume barrier response loss")
                    real_fsync(fd)

                with (
                    mock.patch(
                        "agent_team.recovery.os.fsync",
                        side_effect=fail_once,
                    ),
                    self.assertRaises(RecoveryDurabilityError),
                ):
                    restore_ledger.complete_tombstone_first(
                        orphan,
                        floor_lower_bound=RecoveryFloor(
                            recovery_epoch=1,
                            fencing_token_floor=1,
                        ),
                        owner=owner,
                    )
                self.assertFalse((root / RECOVERY_LEDGER_BASENAME).exists())
                self.assertEqual(tombstone_bytes, tombstone_path.read_bytes())
                completed = restore_ledger.complete_tombstone_first(
                    orphan,
                    floor_lower_bound=RecoveryFloor(
                        recovery_epoch=1,
                        fencing_token_floor=1,
                    ),
                    owner=owner,
                )
                self.assertIsInstance(completed, RestoreHandle)
            finally:
                session.close()

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

    def test_ledger_parser_rejects_nonadjacent_restore_generation(self) -> None:
        prepared = self._record(1, "RESTORE_PREPARED", generation=1)
        replaced = self._record(2, "RESTORE_REPLACED", generation=1)
        committed = self._record(3, "RESTORE_COMMITTED", generation=1)
        skipped = self._record(4, "RESTORE_PREPARED", generation=3)
        raw = b"".join(
            recovery_module._encode_record(record)
            for record in (prepared, replaced, committed, skipped)
        )
        with self.assertRaises((LedgerReadError, RecoveryLedgerError)):
            RecoveryLedgerWriter._latest_from_bytes(raw, allow_empty=False)

    def test_ledger_append_rejects_nonadjacent_restore_generation_before_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-generation-gap-"
        ) as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            prepared = self._record(1, "RESTORE_PREPARED", generation=1)
            replaced = self._record(2, "RESTORE_REPLACED", generation=1)
            committed = self._record(3, "RESTORE_COMMITTED", generation=1)
            skipped = self._record(4, "RESTORE_PREPARED", generation=3)
            writer.initialize(prepared, self._initialization(prepared.backup_digest))
            writer.append(replaced)
            writer.append(committed)
            before = (root / RECOVERY_LEDGER_BASENAME).read_bytes()
            with self.assertRaises(RecoveryLedgerError):
                writer.append(skipped)
            self.assertEqual(before, (root / RECOVERY_LEDGER_BASENAME).read_bytes())

    def test_ledger_append_rejects_replaced_to_aborted_before_write(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-abort-predecessor-"
        ) as temporary:
            root = _root(temporary)
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            prepared = self._record(1, "RESTORE_PREPARED")
            replaced = self._record(2, "RESTORE_REPLACED")
            aborted = self._record(3, "RESTORE_ABORTED")
            writer.initialize(prepared, self._initialization(prepared.backup_digest))
            writer.append(replaced)
            before = (root / RECOVERY_LEDGER_BASENAME).read_bytes()
            with self.assertRaises(RecoveryLedgerError):
                writer.append(aborted)
            self.assertEqual(before, (root / RECOVERY_LEDGER_BASENAME).read_bytes())

    def test_restore_history_rejects_generation_gap_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-generation-gap-history-"
        ) as temporary:
            root = _root(temporary)
            ledger_records = (
                self._record(1, "RESTORE_PREPARED", generation=1),
                self._record(2, "RESTORE_REPLACED", generation=1),
                self._record(3, "RESTORE_COMMITTED", generation=1),
                self._record(4, "RESTORE_PREPARED", generation=3),
            )
            tombstone_first = replace(
                RecoveryTombstoneTest._record(1, "PREPARED", generation=1),
                audit_ref="audit/1",
            )
            tombstone_committed = replace(
                RecoveryTombstoneTest._record(2, "COMMITTED", generation=1),
                audit_ref="audit/1",
            )
            tombstone_skipped = replace(
                RecoveryTombstoneTest._record(3, "PREPARED", generation=3),
                audit_ref="audit/3",
            )
            tombstone_records = (
                tombstone_first,
                tombstone_committed,
                tombstone_skipped,
            )
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            ledger_path.write_bytes(
                b"".join(
                    recovery_module._encode_record(record) for record in ledger_records
                )
            )
            tombstone_path.write_bytes(
                b"".join(
                    recovery_module._encode_tombstone(record)
                    for record in tombstone_records
                )
            )
            ledger_path.chmod(0o600)
            tombstone_path.chmod(0o600)
            before = (ledger_path.read_bytes(), tombstone_path.read_bytes())
            owner = BorrowedRootOwner(root)
            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    RestoreLedger(root, marker_name=MARKER_NAME).read(owner)
            finally:
                owner.close()
            self.assertEqual(
                before, (ledger_path.read_bytes(), tombstone_path.read_bytes())
            )

    def test_restore_history_rejects_replaced_abort_predecessor_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-restore-abort-predecessor-history-"
        ) as temporary:
            root = _root(temporary)
            prepared = self._record(1, "RESTORE_PREPARED")
            replaced = self._record(2, "RESTORE_REPLACED")
            aborted = self._record(3, "RESTORE_ABORTED")
            tombstone_prepared = replace(
                RecoveryTombstoneTest._record(1, "PREPARED"),
                audit_ref="audit/1",
            )
            tombstone_aborted = replace(
                RecoveryTombstoneTest._record(2, "ABORTED"),
                audit_ref="audit/1",
            )
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            tombstone_path = root / RECOVERY_TOMBSTONES_BASENAME
            ledger_path.write_bytes(
                b"".join(
                    recovery_module._encode_record(record)
                    for record in (prepared, replaced, aborted)
                )
            )
            tombstone_path.write_bytes(
                b"".join(
                    recovery_module._encode_tombstone(record)
                    for record in (tombstone_prepared, tombstone_aborted)
                )
            )
            ledger_path.chmod(0o600)
            tombstone_path.chmod(0o600)
            before = (ledger_path.read_bytes(), tombstone_path.read_bytes())
            owner = BorrowedRootOwner(root)
            try:
                with (
                    mock.patch(
                        "agent_team.recovery.QuiescenceOwner", BorrowedRootOwner
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    RestoreLedger(root, marker_name=MARKER_NAME).read(owner)
            finally:
                owner.close()
            self.assertEqual(
                before, (ledger_path.read_bytes(), tombstone_path.read_bytes())
            )

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

    def test_unowned_append_close_uncertainty_is_typed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-close-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            second = self._record(2, "RESTORE_REPLACED")
            writer.initialize(first, self._initialization())
            real_open = os.open
            real_close = os.close
            write_fds: list[int] = []

            def capture_open(*args: object, **kwargs: object) -> int:
                fd = real_open(*args, **kwargs)  # type: ignore[arg-type]
                flags = args[1]
                if type(flags) is int and flags & os.O_WRONLY:
                    write_fds.append(fd)
                return fd

            def fail_write_close(fd: int) -> None:
                if fd in write_fds:
                    raise OSError("simulated unowned close uncertainty")
                real_close(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.recovery.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=fail_write_close,
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    writer.append(second)
                self.assertGreaterEqual(len(writer._orphan_fds), 1)
                writer.close()
                self.assertEqual([], writer._orphan_fds)
            finally:
                for fd in write_fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass

    def test_unowned_unlock_uncertainty_is_typed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-unlock-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            second = self._record(2, "RESTORE_REPLACED")
            writer.initialize(first, self._initialization())
            real_flock = fcntl.flock

            def fail_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    raise OSError("simulated unowned unlock uncertainty")
                real_flock(fd, operation)

            with (
                mock.patch(
                    "agent_team.recovery.fcntl.flock",
                    side_effect=fail_unlock,
                ),
                self.assertRaises(RecoveryLedgerError),
            ):
                writer.append(second)
            self.assertGreaterEqual(len(writer._orphan_sessions), 1)
            writer.close()
            self.assertEqual([], writer._orphan_sessions)

    def test_unowned_append_unlock_base_exception_closes_append_fd(self) -> None:
        class CleanupSignal(BaseException):
            pass

        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-unlock-base-exception-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            second = self._record(2, "RESTORE_REPLACED")
            writer.initialize(first, self._initialization())
            opened_fds: list[int] = []
            real_open = os.open
            real_close = os.close
            real_flock = fcntl.flock

            def capture_open(*args: object, **kwargs: object) -> int:
                fd = real_open(*args, **kwargs)  # type: ignore[arg-type]
                flags = args[1]
                if type(flags) is int and flags & os.O_WRONLY:
                    opened_fds.append(fd)
                return fd

            def fail_unlock(fd: int, operation: int) -> None:
                if operation == fcntl.LOCK_UN:
                    raise CleanupSignal("simulated unlock cleanup signal")
                real_flock(fd, operation)

            try:
                with (
                    mock.patch(
                        "agent_team.recovery.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.recovery.fcntl.flock",
                        side_effect=fail_unlock,
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    writer.append(second)
                self.assertTrue(opened_fds)
                for fd in opened_fds:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
            finally:
                for fd in opened_fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass

    def test_unowned_read_close_uncertainty_is_typed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-read-close-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            writer.initialize(first, self._initialization())
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CLOEXEC,
            )
            real_open = os.open
            real_close = os.close
            read_fds: list[int] = []

            def capture_open(*args: object, **kwargs: object) -> int:
                fd = real_open(*args, **kwargs)  # type: ignore[arg-type]
                flags = args[1]
                if type(flags) is int and not flags & os.O_WRONLY:
                    read_fds.append(fd)
                return fd

            def fail_read_close(fd: int) -> None:
                if fd in read_fds:
                    raise OSError("simulated unowned read close uncertainty")
                real_close(fd)

            try:
                with (
                    mock.patch(
                        "agent_team.recovery.os.open",
                        side_effect=capture_open,
                    ),
                    mock.patch(
                        "agent_team.recovery.os.close",
                        side_effect=fail_read_close,
                    ),
                    self.assertRaises(RecoveryLedgerError),
                ):
                    writer._read_ledger(root_fd)
                self.assertGreaterEqual(len(writer._orphan_fds), 1)
                writer.close()
                self.assertEqual([], writer._orphan_fds)
            finally:
                for fd in read_fds:
                    try:
                        real_close(fd)
                    except OSError:
                        pass
                os.close(root_fd)

    def test_unowned_read_rejects_same_generation_terminal_provenance_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-read-provenance-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            first = self._record(1, "RESTORE_PREPARED")
            writer.initialize(first, self._initialization())
            writer.append(self._record(2, "RESTORE_REPLACED"))
            writer.append(self._record(3, "RESTORE_COMMITTED"))
            ledger_path = root / RECOVERY_LEDGER_BASENAME
            lines = ledger_path.read_bytes().splitlines()
            terminal = cast(dict[str, object], json.loads(lines[-1]))
            terminal["actor"] = "different-actor"
            terminal["audit_ref"] = "audit/different"
            ledger_path.write_bytes(
                b"\n".join(
                    [
                        *lines[:-1],
                        json.dumps(
                            terminal,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    ]
                )
                + b"\n"
            )
            with self.assertRaises(RecoveryLedgerError):
                writer.read()

    def test_normal_open_preflight_retains_uncertain_ledger_and_tombstone_reads(
        self,
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
            audit_ref="audit/preflight-retention",
        )
        replaced_ledger = replace(prepared_ledger, sequence=2, phase="RESTORE_REPLACED")
        committed_ledger = replace(
            prepared_ledger,
            sequence=3,
            phase="RESTORE_COMMITTED",
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
            identities=(),
            actor="operator",
            audit_ref="audit/preflight-retention",
        )
        committed_tombstone = replace(
            prepared_tombstone,
            sequence=2,
            phase="COMMITTED",
        )
        for target_name in (RECOVERY_LEDGER_BASENAME, RECOVERY_TOMBSTONES_BASENAME):
            with (
                self.subTest(target_name=target_name),
                tempfile.TemporaryDirectory(
                    prefix="agent-team-preflight-retention-"
                ) as temporary,
            ):
                root = _root(temporary)
                (root / RECOVERY_LEDGER_BASENAME).write_bytes(
                    b"".join(
                        _encode_record(record)
                        for record in (
                            prepared_ledger,
                            replaced_ledger,
                            committed_ledger,
                        )
                    )
                )
                (root / RECOVERY_TOMBSTONES_BASENAME).write_bytes(
                    b"".join(
                        _encode_tombstone(record)
                        for record in (prepared_tombstone, committed_tombstone)
                    )
                )
                (root / RECOVERY_LEDGER_BASENAME).chmod(0o600)
                (root / RECOVERY_TOMBSTONES_BASENAME).chmod(0o600)
                root_fd = os.open(
                    root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | os.O_CLOEXEC,
                )
                real_open = os.open
                real_close = os.close
                read_fds: list[int] = []
                failed = [False]
                retained: list[tuple[int, tuple[int, int] | None, str]] = []

                def capture_open(
                    *args: object,
                    _real_open: Callable[..., int] = real_open,
                    _read_fds: list[int] = read_fds,
                    _target_name: str = target_name,
                    **kwargs: object,
                ) -> int:
                    fd = _real_open(*args, **kwargs)
                    if args and args[0] == _target_name:
                        _read_fds.append(fd)
                    return fd

                def fail_close(
                    fd: int,
                    _real_close: Callable[[int], None] = real_close,
                    _read_fds: list[int] = read_fds,
                    _failed: list[bool] = failed,
                ) -> None:
                    if fd in _read_fds and not _failed[0]:
                        _failed[0] = True
                        raise OSError("simulated preflight close uncertainty")
                    _real_close(fd)

                def retain(
                    fd: int,
                    expected_identity: tuple[int, int] | None,
                    label: str,
                    _retained: list[tuple[int, tuple[int, int] | None, str]] = retained,
                ) -> None:
                    _retained.append((fd, expected_identity, label))

                try:
                    with (
                        mock.patch(
                            "agent_team.recovery.os.open",
                            side_effect=capture_open,
                        ),
                        mock.patch(
                            "agent_team.recovery.os.close",
                            side_effect=fail_close,
                        ),
                        self.assertRaises(RecoveryLedgerError),
                    ):
                        _normal_open_preflight(root_fd, retain_fd=retain)
                    self.assertEqual(1, len(retained))
                    self.assertIsNotNone(retained[0][1])
                    os.fstat(retained[0][0])
                finally:
                    for fd, _, _ in retained:
                        try:
                            real_close(fd)
                        except OSError:
                            pass
                    os.close(root_fd)

    def test_unowned_reader_filesystem_close_uncertainty_is_typed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-ledger-reader-close-"
        ) as temporary:
            root = _root(temporary)
            with CoordinationStore(root):
                pass
            writer = RecoveryLedgerWriter(root, marker_name=MARKER_NAME)
            writer.initialize(
                self._record(1, "RESTORE_PREPARED"), self._initialization()
            )
            with (
                mock.patch.object(
                    StateFilesystem,
                    "close",
                    side_effect=OSError(
                        "simulated reader filesystem close uncertainty"
                    ),
                ),
                self.assertRaises(RecoveryLedgerError),
            ):
                writer.read()
            self.assertEqual(1, len(writer._orphan_filesystems))
            writer.close()
            self.assertEqual([], writer._orphan_filesystems)


class RecoveryLayoutTest(unittest.TestCase):
    def test_layout_is_frozen_and_canonical(self) -> None:
        layout = RecoveryLayout(marker_name=MARKER_NAME)
        self.assertEqual(RECOVERY_LEDGER_BASENAME, layout.ledger_name)
        with self.assertRaises(FrozenInstanceError):
            layout.ledger_name = "alternate.ledger"  # type: ignore[misc]

    def test_coordinator_layout_cannot_be_mutated_or_bypass_quiescence(self) -> None:
        clock = FakeClock()
        temporary, store = _store(clock)
        try:
            with self.assertRaises(RecoveryLedgerError):
                RecoveryLedgerWriter(
                    store.state_root,
                    marker_name=MARKER_NAME,
                ).initialize(
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
                        request_digest="sha256:" + "a" * 64,
                    ),
                )
            coordinator = RecoveryCoordinator(store, marker_name=MARKER_NAME)
            with self.assertRaises(AttributeError):
                coordinator.ledger_name = "alternate.ledger"  # type: ignore[misc]
            with self.assertRaises(AttributeError):
                coordinator.marker_name = "alternate.marker"  # type: ignore[misc]
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
