"""Bind the native team controller to a private Zellij terminal."""

from __future__ import annotations

from pathlib import Path

from .native_backend import NativeBackend
from .zellij import ZELLIJ_CONTRACT_VERSION, ZellijDriver, ZellijReceipt


class ZellijBackend(NativeBackend[ZellijReceipt]):
    def __init__(
        self,
        *,
        zellij_executable: str | Path = "zellij",
        launcher_path: Path | None = None,
        resume_existing: bool = False,
    ) -> None:
        self._zellij_executable = zellij_executable
        super().__init__(launcher_path=launcher_path, resume_existing=resume_existing)

    @property
    def runtime(self) -> str:
        return "zellij"

    def _new_driver(
        self, socket_root: Path, run_nonce: str, session_name: str
    ) -> ZellijDriver:
        return ZellijDriver(
            self._zellij_executable, socket_root, run_nonce, session_name
        )

    def _startup_socket_path(self, socket_root: Path, session_name: str) -> Path:
        return socket_root / "s" / ZELLIJ_CONTRACT_VERSION / session_name

    def _parse_receipt(self, value: object) -> ZellijReceipt:
        return ZellijReceipt.from_dict(value)

    def _restore_driver(self, receipt: ZellijReceipt) -> ZellijDriver:
        return ZellijDriver.from_receipt(receipt)

    def _socket_root(self, receipt: ZellijReceipt) -> Path:
        return receipt.private_root
