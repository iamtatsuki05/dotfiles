"""Typed lease and provider-proof contracts for the coordination store.

The values in this module carry only opaque identities and bounded proof
references.  Provider payloads, raw responses, and SQLite objects are
deliberately outside the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal, NoReturn, Protocol

SQLITE_INTEGER_MAX: Final[int] = 2**63 - 1
MAX_IDENTIFIER_LENGTH: Final[int] = 128
MAX_PROOF_REF_LENGTH: Final[int] = 128

LeasePhase = Literal["FENCE_PENDING", "CLAIMED"]
RecoveryRebaseMode = Literal["INTENT", "RECEIPTED", "COMPLETED"]
ProviderLifecycleStatus = Literal["ABSENT", "COMPLETED", "UNKNOWN"]
ProviderConsistency = Literal["STRONG", "UNKNOWN"]

_IDENTIFIER = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_IDENTIFIER_LENGTH - 1}}}\Z"
)
_PROOF_REF = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:/-]{{0,{MAX_PROOF_REF_LENGTH - 1}}}\Z"
)
_SECRET_LIKE = re.compile(
    r"(?:api[_-]?key|secret|token|password|passwd|authorization|bearer|"
    r"cookie|credential|private[_-]?key|raw[_-]?response)",
    re.IGNORECASE,
)
_RECEIPT_SENTINEL = object()
_EFFECT_SENTINEL = object()
_FLOOR_SENTINEL = object()


class LeaseError(RuntimeError):
    """Base class for lease/provider contract failures."""


class LeaseConflictError(LeaseError):
    """The requested mutation does not match the current lease identity."""


class ClockRollbackError(LeaseError):
    """A supplied timestamp moved behind the durable store clock."""


class ProviderBlockedError(LeaseError):
    """The provider cannot provide the required fencing contract."""


class ProviderProofError(LeaseError, ValueError):
    """A provider fence proof or status is not strong and identity-safe."""


class ProviderReceiptError(LeaseError, ValueError):
    """A provider receipt failed provenance or identity verification."""


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(f"{field} must be an opaque identifier")
    if _SECRET_LIKE.search(value):
        _fail(f"{field} must be an opaque identifier")
    return value


def _proof_ref(value: object) -> str:
    if type(value) is not str or _PROOF_REF.fullmatch(value) is None:
        _fail("proof_ref must be a bounded opaque reference")
    if _SECRET_LIKE.search(value):
        _fail("proof_ref must be a bounded opaque reference")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > SQLITE_INTEGER_MAX:
        _fail(f"{field} must be a supported integer")
    return value


def _choice(value: object, field: str, choices: tuple[str, ...]) -> str:
    if type(value) is not str or value not in choices:
        _fail(f"{field} is unsupported")
    return value


def _same_identity(left: object, right: object, field: str) -> None:
    if left != right:
        raise ProviderReceiptError(f"provider {field} does not match effect")


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Capabilities required before a provider can be used for an effect."""

    idempotency: bool
    fencing: bool
    strong_status: bool

    def __post_init__(self) -> None:
        if type(self.idempotency) is not bool:
            _fail("idempotency capability must be a boolean")
        if type(self.fencing) is not bool:
            _fail("fencing capability must be a boolean")
        if type(self.strong_status) is not bool:
            _fail("strong_status capability must be a boolean")


@dataclass(frozen=True, slots=True)
class RecoveryFloor:
    """Durable recovery epoch and global fencing-token floor observation."""

    recovery_epoch: int
    fencing_token_floor: int

    def __post_init__(self) -> None:
        _integer(self.recovery_epoch, "recovery_epoch")
        _integer(self.fencing_token_floor, "fencing_token_floor")


@dataclass(frozen=True, slots=True, init=False)
class RecoveryFloorReservation:
    """Opaque store-issued reservation for an atomic recovery-floor advance."""

    recovery_epoch: int
    fencing_token_floor: int
    _provenance: object = field(default=None, repr=False, compare=False)

    @property
    def is_issued(self) -> bool:
        return getattr(self, "_provenance", None) is _FLOOR_SENTINEL


@dataclass(frozen=True, slots=True)
class ProviderFenceProof:
    """Opaque proof that a provider reserved this exact lease fence."""

    operation_id: str
    effect_key: str
    provider_id: str
    owner: str
    attempt: int
    lease_epoch: int
    fencing_token: int
    proof_version: int
    proof_ref: str
    consistency: ProviderConsistency = "STRONG"

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.effect_key, "effect_key")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.owner, "owner")
        _integer(self.attempt, "attempt", minimum=1)
        _integer(self.lease_epoch, "lease_epoch")
        _integer(self.fencing_token, "fencing_token", minimum=1)
        _integer(self.proof_version, "proof_version", minimum=1)
        _proof_ref(self.proof_ref)
        _choice(self.consistency, "consistency", ("STRONG", "UNKNOWN"))
        if self.consistency != "STRONG":
            raise ProviderProofError("provider fence proof is not strongly consistent")


@dataclass(frozen=True, slots=True)
class Claim:
    """Immutable lease identity used for every subsequent store mutation."""

    operation_id: str
    effect_key: str
    provider_id: str
    owner: str
    attempt: int
    lease_epoch: int
    fencing_token: int
    lease_heartbeat_ns: int
    lease_expires_ns: int
    phase: LeasePhase
    fence_proof: ProviderFenceProof | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.effect_key, "effect_key")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.owner, "owner")
        _integer(self.attempt, "attempt", minimum=1)
        _integer(self.lease_epoch, "lease_epoch")
        _integer(self.fencing_token, "fencing_token", minimum=1)
        _integer(self.lease_heartbeat_ns, "lease_heartbeat_ns")
        _integer(self.lease_expires_ns, "lease_expires_ns")
        phase = _choice(self.phase, "phase", ("FENCE_PENDING", "CLAIMED"))
        if self.lease_expires_ns <= self.lease_heartbeat_ns:
            _fail("lease_expires_ns must be after lease_heartbeat_ns")
        if phase == "FENCE_PENDING" and self.fence_proof is not None:
            _fail("FENCE_PENDING claim must not contain a fence proof")
        if phase == "CLAIMED":
            if self.fence_proof is None:
                _fail("CLAIMED claim requires a fence proof")
            _validate_proof_identity(self, self.fence_proof)

    @property
    def recovery_epoch(self) -> int:
        """The C2 lease epoch is the current durable recovery epoch."""

        return self.lease_epoch


@dataclass(frozen=True, slots=True, init=False)
class ProviderEffect:
    """Identity-only provider request issued after a durable effect prepare."""

    operation_id: str
    effect_key: str
    provider_id: str
    owner: str
    attempt: int
    lease_epoch: int
    fencing_token: int
    fence_proof: ProviderFenceProof | None = None
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ProviderEffect instances are store-issued")

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.effect_key, "effect_key")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.owner, "owner")
        _integer(self.attempt, "attempt", minimum=1)
        _integer(self.lease_epoch, "lease_epoch")
        _integer(self.fencing_token, "fencing_token", minimum=1)
        if self.fence_proof is not None:
            _validate_proof_identity(self, self.fence_proof)

    @property
    def is_issued(self) -> bool:
        return getattr(self, "_provenance", None) is _EFFECT_SENTINEL


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Strong/unknown provider observation without raw provider response data."""

    operation_id: str
    effect_key: str
    provider_id: str
    owner: str
    attempt: int
    lease_epoch: int
    fencing_token: int
    provider_effect_id: str | None
    status: ProviderLifecycleStatus
    consistency: ProviderConsistency
    proof_version: int | None = None
    proof_ref: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.effect_key, "effect_key")
        _identifier(self.provider_id, "provider_id")
        _identifier(self.owner, "owner")
        _integer(self.attempt, "attempt", minimum=1)
        _integer(self.lease_epoch, "lease_epoch")
        _integer(self.fencing_token, "fencing_token", minimum=1)
        if self.provider_effect_id is not None:
            _identifier(self.provider_effect_id, "provider_effect_id")
        _choice(self.status, "status", ("ABSENT", "COMPLETED", "UNKNOWN"))
        _choice(self.consistency, "consistency", ("STRONG", "UNKNOWN"))
        if self.consistency == "UNKNOWN" and self.status == "ABSENT":
            raise ProviderProofError(
                "unknown provider consistency cannot report ABSENT"
            )
        if self.status == "ABSENT" and self.provider_effect_id is not None:
            _fail("ABSENT status must not contain provider_effect_id")
        if self.status == "COMPLETED" and self.provider_effect_id is None:
            _fail("COMPLETED status requires provider_effect_id")
        if self.proof_version is not None:
            _integer(self.proof_version, "proof_version", minimum=1)
        if self.proof_ref is not None:
            _proof_ref(self.proof_ref)
        if (self.proof_version is None) != (self.proof_ref is None):
            _fail("proof_version and proof_ref must be provided together")


@dataclass(frozen=True, slots=True, init=False)
class VerifiedProviderReceipt:
    """Receipt whose provenance was checked against an effect and fence proof.

    There is intentionally no public field constructor.  Store internals
    issue receipts only after checking all identities and strong status.
    """

    operation_id: str
    effect_key: str
    provider_id: str
    owner: str
    attempt: int
    lease_epoch: int
    fencing_token: int
    provider_effect_id: str
    provider_status: str
    proof_version: int
    proof_ref: str
    _provenance: object = field(default=None, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("VerifiedProviderReceipt instances are store-issued")

    @property
    def status(self) -> str:
        return self.provider_status

    @property
    def is_verified(self) -> bool:
        return getattr(self, "_provenance", None) is _RECEIPT_SENTINEL


_RECOVERY_STATUS_VALUES: Final[tuple[str, ...]] = (
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
    "RESTORE_INCOMPLETE",
)


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Validated current-operation view consumed by future recovery children."""

    operation_id: str
    effect_key: str
    provider_id: str | None
    status: str
    updated_ns: int
    current_attempt: int
    recovery_epoch: int
    owner: str | None
    lease_heartbeat_ns: int | None
    lease_expires_ns: int | None
    lease_epoch: int
    fencing_token: int
    fence_proof_version: int | None
    fence_proof_ref: str | None
    effect_started_ns: int | None
    fence_started_ns: int | None
    verified_receipt_identity: VerifiedProviderReceipt | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        _identifier(self.effect_key, "effect_key")
        if self.provider_id is not None:
            _identifier(self.provider_id, "provider_id")
        _choice(self.status, "status", _RECOVERY_STATUS_VALUES)
        _integer(self.updated_ns, "updated_ns")
        _integer(self.current_attempt, "current_attempt")
        _integer(self.recovery_epoch, "recovery_epoch")
        if self.owner is not None:
            _identifier(self.owner, "owner")
        for value, field_name in (
            (self.lease_heartbeat_ns, "lease_heartbeat_ns"),
            (self.lease_expires_ns, "lease_expires_ns"),
            (self.effect_started_ns, "effect_started_ns"),
            (self.fence_started_ns, "fence_started_ns"),
        ):
            if value is not None:
                _integer(value, field_name)
        _integer(self.lease_epoch, "lease_epoch")
        _integer(self.fencing_token, "fencing_token")
        if self.fence_proof_version is not None:
            _integer(self.fence_proof_version, "fence_proof_version", minimum=1)
        if self.fence_proof_ref is not None:
            _proof_ref(self.fence_proof_ref)
        if (self.fence_proof_version is None) != (self.fence_proof_ref is None):
            _fail("fence proof fields must be provided together")
        if self.current_attempt == 0:
            if (
                any(
                    value is not None
                    for value in (
                        self.owner,
                        self.lease_heartbeat_ns,
                        self.lease_expires_ns,
                        self.fence_proof_version,
                        self.fence_proof_ref,
                        self.effect_started_ns,
                        self.fence_started_ns,
                    )
                )
                or self.fencing_token != 0
            ):
                _fail("attempt zero cannot contain lease identity")
        elif (
            self.provider_id is None
            or self.owner is None
            or self.lease_heartbeat_ns is None
            or self.lease_expires_ns is None
            or self.fencing_token < 1
        ):
            _fail("current lease identity is incomplete")
        if (
            self.lease_heartbeat_ns is not None
            and self.lease_expires_ns is not None
            and self.lease_expires_ns <= self.lease_heartbeat_ns
        ):
            _fail("lease_expires_ns must be after lease_heartbeat_ns")
        receipt = self.verified_receipt_identity
        if receipt is not None:
            if self.status not in {"RECEIPTED", "COMPLETED"}:
                _fail("verified receipt requires a receipted operation")
            if (
                receipt.operation_id != self.operation_id
                or receipt.effect_key != self.effect_key
                or receipt.provider_id != self.provider_id
                or receipt.owner != self.owner
                or receipt.attempt != self.current_attempt
                or receipt.lease_epoch != self.lease_epoch
                or receipt.fencing_token != self.fencing_token
            ):
                _fail("verified receipt identity does not match snapshot")


def _issue_provider_effect(
    *,
    operation_id: str,
    effect_key: str,
    provider_id: str,
    owner: str,
    attempt: int,
    lease_epoch: int,
    fencing_token: int,
    fence_proof: ProviderFenceProof | None = None,
) -> ProviderEffect:
    values = {
        "operation_id": operation_id,
        "effect_key": effect_key,
        "provider_id": provider_id,
        "owner": owner,
        "attempt": attempt,
        "lease_epoch": lease_epoch,
        "fencing_token": fencing_token,
        "fence_proof": fence_proof,
    }
    instance = object.__new__(ProviderEffect)
    for field_name, value in values.items():
        object.__setattr__(instance, field_name, value)
    instance.__post_init__()
    object.__setattr__(instance, "_provenance", _EFFECT_SENTINEL)
    return instance


def _issue_floor_reservation(
    recovery_epoch: int,
    fencing_token_floor: int,
) -> RecoveryFloorReservation:
    values = {
        "recovery_epoch": recovery_epoch,
        "fencing_token_floor": fencing_token_floor,
    }
    instance = object.__new__(RecoveryFloorReservation)
    for field_name, value in values.items():
        object.__setattr__(instance, field_name, value)
    RecoveryFloor(recovery_epoch, fencing_token_floor)
    object.__setattr__(instance, "_provenance", _FLOOR_SENTINEL)
    return instance


def _verified_receipt_from_status(
    effect: ProviderEffect,
    proof: ProviderFenceProof,
    status: ProviderStatus,
) -> VerifiedProviderReceipt:
    if not isinstance(effect, ProviderEffect):
        raise ProviderReceiptError("provider effect has an unsupported type")
    if not effect.is_issued:
        raise ProviderReceiptError("provider effect provenance is unverified")
    if not isinstance(status, ProviderStatus):
        raise ProviderReceiptError("provider status has an unsupported type")
    _validate_proof_identity(effect, proof)
    if status.status != "COMPLETED" or status.consistency != "STRONG":
        raise ProviderReceiptError(
            "provider receipt requires a strongly consistent completed status"
        )
    _validate_status_identity(effect, status)
    provider_effect_id = status.provider_effect_id
    if provider_effect_id is None:
        raise ProviderReceiptError("provider receipt is missing effect identity")
    if status.proof_version is not None and status.proof_version != proof.proof_version:
        raise ProviderReceiptError("provider proof version does not match status")
    if status.proof_ref is not None and status.proof_ref != proof.proof_ref:
        raise ProviderReceiptError("provider proof reference does not match status")
    values = {
        "operation_id": effect.operation_id,
        "effect_key": effect.effect_key,
        "provider_id": effect.provider_id,
        "owner": effect.owner,
        "attempt": effect.attempt,
        "lease_epoch": effect.lease_epoch,
        "fencing_token": effect.fencing_token,
        "provider_effect_id": provider_effect_id,
        "provider_status": status.status,
        "proof_version": proof.proof_version,
        "proof_ref": proof.proof_ref,
        "_provenance": _RECEIPT_SENTINEL,
    }
    instance = object.__new__(VerifiedProviderReceipt)
    for field_name, value in values.items():
        object.__setattr__(instance, field_name, value)
    return instance


class ProviderPort(Protocol):
    """Trusted composition-root adapter with provider-side high-water checks.

    The caller cannot select an adapter from task data.  This seam is a
    trust boundary for in-process code, not a cryptographic defense against
    malicious code that can introspect the same Python process.
    """

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def reserve_fence(self, effect: ProviderEffect) -> ProviderFenceProof: ...

    def execute(self, effect: ProviderEffect) -> ProviderStatus: ...

    def status(self, effect: ProviderEffect) -> ProviderStatus: ...


class LeaseAuthority(Protocol):
    """Minimal typed lease authority seam used by future runtime layers."""

    def claim(
        self,
        operation_id: str,
        *,
        owner: str,
        provider_id: str,
        lease_ttl_ns: int | None = None,
        effect_key: str | None = None,
        now_ns: int | None = None,
    ) -> Claim: ...

    def heartbeat(
        self,
        claim: Claim,
        *,
        lease_ttl_ns: int | None = None,
        now_ns: int | None = None,
    ) -> Claim: ...

    def reclaim(
        self,
        claim: Claim | str,
        *,
        owner: str | None = None,
        provider_id: str | None = None,
        effect_key: str | None = None,
        lease_ttl_ns: int | None = None,
        now_ns: int | None = None,
    ) -> Claim: ...

    def reserve_fence(self, claim: Claim, provider: ProviderPort) -> Claim: ...

    def execute_effect(
        self,
        claim: Claim,
        provider: ProviderPort,
        *,
        now_ns: int | None = None,
    ) -> VerifiedProviderReceipt: ...

    def complete(
        self, receipt: VerifiedProviderReceipt, *, now_ns: int | None = None
    ) -> object: ...


def require_provider_capabilities(provider: object) -> ProviderCapabilities:
    """Return capabilities or fail before any provider call is made."""

    if any(
        not callable(getattr(provider, method, None))
        for method in ("reserve_fence", "execute", "status")
    ):
        raise ProviderBlockedError("provider port is incomplete")
    capabilities = getattr(provider, "capabilities", None)
    if isinstance(capabilities, ProviderCapabilities):
        result = capabilities
    else:
        # Accept structurally equivalent adapters without treating missing
        # attributes as safe defaults.  Missing capability means blocked.
        values: list[bool] = []
        for name in ("idempotency", "fencing", "strong_status"):
            value = getattr(capabilities, name, None)
            if type(value) is not bool:
                raise ProviderBlockedError("provider capabilities are incomplete")
            values.append(value)
        result = ProviderCapabilities(*values)
    if not (result.idempotency and result.fencing and result.strong_status):
        raise ProviderBlockedError("provider lacks required effect capabilities")
    return result


def _validate_proof_identity(effect: object, proof: ProviderFenceProof) -> None:
    if not isinstance(proof, ProviderFenceProof):
        raise ProviderProofError("provider fence proof has an unsupported type")
    for field_name in (
        "operation_id",
        "effect_key",
        "provider_id",
        "owner",
        "attempt",
        "lease_epoch",
        "fencing_token",
    ):
        _same_identity(
            getattr(effect, field_name), getattr(proof, field_name), field_name
        )


def _validate_status_identity(effect: ProviderEffect, status: ProviderStatus) -> None:
    if not isinstance(status, ProviderStatus):
        raise ProviderReceiptError("provider status has an unsupported type")
    for field_name in (
        "operation_id",
        "effect_key",
        "provider_id",
        "owner",
        "attempt",
        "lease_epoch",
        "fencing_token",
    ):
        _same_identity(
            getattr(effect, field_name), getattr(status, field_name), field_name
        )


__all__ = [
    "Claim",
    "ClockRollbackError",
    "LeaseAuthority",
    "LeaseConflictError",
    "LeaseError",
    "ProviderBlockedError",
    "ProviderCapabilities",
    "ProviderConsistency",
    "ProviderEffect",
    "ProviderFenceProof",
    "ProviderLifecycleStatus",
    "ProviderPort",
    "ProviderProofError",
    "ProviderReceiptError",
    "ProviderStatus",
    "RecoveryFloor",
    "RecoveryRebaseMode",
    "RecoverySnapshot",
    "VerifiedProviderReceipt",
    "require_provider_capabilities",
]
