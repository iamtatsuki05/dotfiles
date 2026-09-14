"""The terminal evidence consumed by the native team controller."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, Protocol, TypeGuard, TypeVar

NATIVE_RUNTIMES: Final = frozenset({"tmux", "herdr", "zellij"})
TerminalPresence = Literal["present", "absent", "unknown"]


def is_native_runtime(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and value in NATIVE_RUNTIMES


class NativeTerminalReceipt(Protocol):
    @property
    def run_nonce(self) -> str: ...

    @property
    def session_name(self) -> str: ...

    @property
    def pane_pid(self) -> int: ...

    @property
    def server_pid(self) -> int: ...

    def as_dict(self) -> dict[str, object]: ...


class NativeTerminalInspection(Protocol):
    @property
    def presence(self) -> TerminalPresence: ...

    @property
    def running(self) -> bool | None: ...

    @property
    def exit_status(self) -> int | None: ...

    @property
    def identity_verified(self) -> bool: ...

    @property
    def pane_present(self) -> bool: ...

    @property
    def session_present(self) -> bool | None: ...

    @property
    def pane_pid(self) -> int | None: ...

    @property
    def server_pid(self) -> int | None: ...

    @property
    def observed_nonce(self) -> str | None: ...

    @property
    def reason(self) -> str | None: ...


class NativeTerminalCloseResult(Protocol):
    @property
    def evidence(self) -> str: ...

    @property
    def session_terminated(self) -> bool: ...

    @property
    def server_terminated(self) -> bool: ...

    @property
    def socket_removed(self) -> bool: ...

    @property
    def ownership_verified(self) -> bool: ...


ReceiptT = TypeVar("ReceiptT", bound=NativeTerminalReceipt)


class NativeTerminalDriver(Protocol[ReceiptT]):
    def create(
        self,
        argv: tuple[str, ...],
        cwd: str | Path,
        env: Mapping[str, str],
        title: str,
    ) -> ReceiptT: ...

    def inspect(self, receipt: ReceiptT) -> NativeTerminalInspection: ...

    def attach_argv(self, receipt: ReceiptT) -> tuple[str, ...]: ...

    def close(self, receipt: ReceiptT) -> NativeTerminalCloseResult: ...
