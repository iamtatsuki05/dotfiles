"""Bind the native team controller to a private Herdr terminal."""

from __future__ import annotations

from pathlib import Path

from .herdr import HerdrDriver, HerdrReceipt
from .native_backend import NativeBackend


class HerdrBackend(NativeBackend[HerdrReceipt]):
    def __init__(
        self,
        *,
        herdr_executable: str | Path = "herdr",
        launcher_path: Path | None = None,
        resume_existing: bool = False,
    ) -> None:
        self._herdr_executable = herdr_executable
        super().__init__(launcher_path=launcher_path, resume_existing=resume_existing)

    @property
    def runtime(self) -> str:
        return "herdr"

    def _new_driver(
        self, socket_root: Path, run_nonce: str, session_name: str
    ) -> HerdrDriver:
        return HerdrDriver(self._herdr_executable, socket_root, run_nonce, session_name)

    def _startup_socket_path(self, socket_root: Path, session_name: str) -> Path:
        return socket_root / "c" / "herdr" / "sessions" / session_name / "herdr.sock"

    def _parse_receipt(self, value: object) -> HerdrReceipt:
        return HerdrReceipt.from_dict(value)

    def _restore_driver(self, receipt: HerdrReceipt) -> HerdrDriver:
        return HerdrDriver.from_receipt(receipt)

    def _socket_root(self, receipt: HerdrReceipt) -> Path:
        return receipt.private_root
