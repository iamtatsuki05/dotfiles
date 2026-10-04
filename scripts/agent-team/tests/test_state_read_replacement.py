from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from test_named_runtime_state import _valid_named_state

from agent_team import native_main, runtime


class StateReadReplacementTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="agent-team-state-read-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = _valid_named_state(self.root)
        native = cast(dict[str, object], self.state["native"])
        native["phase"] = "starting"
        self.path = Path(cast(str, self.state["state_path"]))
        runtime.write_state(self.path, self.state)
        self.latest = {**self.state, "native": {**native, "phase": "running"}}

    def assert_closed(self, descriptors: list[int]) -> None:
        for descriptor in descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_atomic_publication_during_open_reads_the_new_private_state(self) -> None:
        attempts = 0
        descriptors: list[int] = []

        def opening(path: Path, flags: int, mode: int = 0o777) -> int:
            nonlocal attempts
            if path == self.path:
                attempts += 1
                if attempts == 1:
                    runtime.write_state(self.path, self.latest, require_existing=True)
            descriptor = os.open(path, flags, mode)
            if path == self.path:
                descriptors.append(descriptor)
            return descriptor

        with patch.object(
            runtime, "os", SimpleNamespace(**{**vars(os), "open": opening})
        ):
            saved = runtime.read_state(self.path)
        self.assertEqual(saved, self.latest)
        self.assertEqual(attempts, 2)
        self.assert_closed(descriptors)

    def test_supervisor_survives_the_starting_to_running_publication(self) -> None:
        published = False

        def opening(path: Path, flags: int, mode: int = 0o777) -> int:
            nonlocal published
            if path == self.path and not published:
                published = True
                runtime.write_state(self.path, self.latest, require_existing=True)
            return os.open(path, flags, mode)

        with patch.object(
            runtime, "os", SimpleNamespace(**{**vars(os), "open": opening})
        ):
            ready = native_main._load_ready_state(self.path, str(self.state["run_id"]))
        self.assertEqual(ready, self.latest)

    def test_continuous_replacement_stops_after_three_attempts_and_closes_fds(
        self,
    ) -> None:
        attempts = 0
        descriptors: list[int] = []

        def opening(path: Path, flags: int, mode: int = 0o777) -> int:
            nonlocal attempts
            if path == self.path:
                attempts += 1
                runtime.write_state(self.path, self.latest, require_existing=True)
            descriptor = os.open(path, flags, mode)
            if path == self.path:
                descriptors.append(descriptor)
            return descriptor

        with (
            patch.object(
                runtime, "os", SimpleNamespace(**{**vars(os), "open": opening})
            ),
            self.assertRaisesRegex(
                runtime.RuntimeValidationError, "changed during open"
            ),
        ):
            runtime.read_state(self.path)
        self.assertEqual(attempts, 3)
        self.assert_closed(descriptors)

    def test_unsafe_or_malformed_replacement_still_fails_closed(self) -> None:
        for damage, message, expected_attempts in (
            ("mode", "state must have mode 0600", 1),
            ("symlink", "state is unavailable", 1),
            ("json", "state is invalid", 2),
        ):
            with self.subTest(damage=damage):
                if self.path.is_symlink():
                    self.path.unlink()
                self.path.write_text(json.dumps(self.state))
                self.path.chmod(0o600)
                replacement = self.root / "replacement.json"
                replacement.write_text(
                    "invalid" if damage == "json" else json.dumps(self.state)
                )
                replacement.chmod(0o644 if damage == "mode" else 0o600)
                opened = False
                attempts = 0

                def opening(
                    path: Path,
                    flags: int,
                    mode: int = 0o777,
                    *,
                    damage: str = damage,
                    replacement: Path = replacement,
                ) -> int:
                    nonlocal opened, attempts
                    if path == self.path:
                        attempts += 1
                    if path == self.path and not opened:
                        opened = True
                        if damage == "symlink":
                            link = self.root / "replacement-link"
                            link.symlink_to(replacement)
                            os.replace(link, self.path)
                        else:
                            os.replace(replacement, self.path)
                    return os.open(path, flags, mode)

                with (
                    patch.object(
                        runtime, "os", SimpleNamespace(**{**vars(os), "open": opening})
                    ),
                    self.assertRaisesRegex(runtime.RuntimeValidationError, message),
                ):
                    runtime.read_state(self.path)
                self.assertEqual(attempts, expected_attempts)

    def test_wrong_owner_is_rejected_before_open(self) -> None:
        open_file = Mock(side_effect=AssertionError("unexpected open"))
        with (
            patch.object(
                runtime,
                "os",
                SimpleNamespace(
                    **{
                        **vars(os),
                        "getuid": lambda: os.getuid() + 1,
                        "open": open_file,
                    }
                ),
            ),
            self.assertRaisesRegex(runtime.RuntimeValidationError, "owner"),
        ):
            runtime.read_state(self.path)
        open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
