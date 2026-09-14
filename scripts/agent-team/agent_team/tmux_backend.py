"""Bind the native team controller to a private tmux terminal."""

from __future__ import annotations

from pathlib import Path

from .native_backend import NativeBackend
from .tmux import TmuxDriver, TmuxReceipt


class TmuxBackend(NativeBackend[TmuxReceipt]):
    def __init__(
        self,
        *,
        tmux_executable: str | Path = "tmux",
        launcher_path: Path | None = None,
        resume_existing: bool = False,
    ) -> None:
        self._tmux_executable = tmux_executable
        super().__init__(launcher_path=launcher_path, resume_existing=resume_existing)

    @property
    def runtime(self) -> str:
        return "tmux"

    def _new_driver(
        self, socket_root: Path, run_nonce: str, session_name: str
    ) -> TmuxDriver:
        return TmuxDriver(
            self._tmux_executable, socket_root / "s", run_nonce, session_name
        )

    def _parse_receipt(self, value: object) -> TmuxReceipt:
        return TmuxReceipt.from_dict(value)

    def _startup_socket_path(self, socket_root: Path, session_name: str) -> Path:
        return socket_root / "s"

    def _restore_driver(self, receipt: TmuxReceipt) -> TmuxDriver:
        return TmuxDriver.from_receipt(receipt)

    def _socket_root(self, receipt: TmuxReceipt) -> Path:
        return receipt.socket_path.parent
