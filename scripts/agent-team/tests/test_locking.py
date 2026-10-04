from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_team import locking
from agent_team.contracts import ErrorCode, RuntimeFailure


class LifecycleReservationPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "team"
        self.root.mkdir(mode=0o700)
        self.state_path = self.root / "state.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_publication_acquire_waits_for_real_holder_release(self) -> None:
        holder = locking._LifecycleReservation(self.state_path, create_parent=True)
        holder.acquire()
        release_started = threading.Event()

        def release_holder() -> None:
            release_started.set()
            time.sleep(0.05)
            holder.release()

        release_thread = threading.Thread(target=release_holder)
        release_thread.start()
        release_started.wait()
        waiter = locking._LifecycleReservation(self.state_path)
        try:
            waiter.acquire_for_publication()
            self.assertIsNotNone(waiter._fd)
        finally:
            waiter.release()
            release_thread.join(timeout=1.0)
        self.assertFalse(release_thread.is_alive())

    def test_publication_acquire_expires_on_real_contention(self) -> None:
        holder = locking._LifecycleReservation(self.state_path, create_parent=True)
        holder.acquire()
        waiter = locking._LifecycleReservation(self.state_path)
        try:
            with (
                self.assertRaises(RuntimeFailure) as raised,
                mock.patch.object(locking, "_PUBLICATION_WAIT_SECONDS", 0.05),
            ):
                waiter.acquire_for_publication()
            self.assertIs(raised.exception.code, ErrorCode.TEAM_ALREADY_RUNNING)
            self.assertIsNone(waiter._fd)
        finally:
            waiter.release()
            holder.release()

    def test_publication_acquire_preserves_immediate_noncontention_error(self) -> None:
        missing = self.root / "missing" / "state.json"
        waiter = locking._LifecycleReservation(missing)
        with (
            self.assertRaises(RuntimeFailure) as raised,
            mock.patch("agent_team.locking.time.sleep") as sleep,
            mock.patch.object(waiter, "acquire", wraps=waiter.acquire) as acquire,
        ):
            waiter.acquire_for_publication()
        self.assertIs(raised.exception.code, ErrorCode.TEAM_NOT_RUNNING)
        sleep.assert_not_called()
        acquire.assert_called_once_with()

    def test_publication_acquire_rejects_double_acquire_without_replacing_fd(
        self,
    ) -> None:
        reservation = locking._LifecycleReservation(self.state_path, create_parent=True)
        reservation.acquire()
        fd = reservation._fd
        try:
            with self.assertRaises(RuntimeFailure) as raised:
                reservation.acquire_for_publication()
            self.assertIs(raised.exception.code, ErrorCode.BACKEND_PROTOCOL_FAILURE)
            self.assertEqual(reservation._fd, fd)
        finally:
            reservation.release()
