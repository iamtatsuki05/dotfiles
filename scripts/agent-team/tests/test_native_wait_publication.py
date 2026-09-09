from __future__ import annotations

import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import test_native_backend as legacy_fixtures
import test_parallel_native_backend as parallel_fixtures

from agent_team import native_backend as native
from agent_team.contracts import (
    ErrorCode,
    EventKind,
    NodeRef,
    Outcome,
    Role,
    RoleTarget,
    RoleWait,
    RuntimeFailure,
    WaitReceipt,
)


@dataclass
class _Harness:
    fixture: Any
    backend: Any
    path: Path
    role: RoleTarget
    role_id: str
    parallel: bool


@contextmanager
def _legacy_harness() -> Iterator[_Harness]:
    fixture = legacy_fixtures.NativeBackendTest("runTest")
    fixture.setUp()
    context = fixture.planner_backend(task_specs=(fixture.task_spec(),))
    backend = context.__enter__()
    try:
        fixture.dispatch_task(backend, Role.PLANNER, fixture.task_spec())
        yield _Harness(
            fixture=fixture,
            backend=backend,
            path=fixture.state_path,
            role=Role.PLANNER,
            role_id=Role.PLANNER.value,
            parallel=False,
        )
    finally:
        context.__exit__(None, None, None)
        fixture.tearDown()
        fixture.doCleanups()


@contextmanager
def _parallel_harness() -> Iterator[_Harness]:
    fixture = parallel_fixtures.ParallelNativeBackendTest("runTest")
    fixture.setUp()
    try:
        fixture.start()
        fixture.dispatch(fixture.named.worker_a, fixture.named.task_a)
        role = fixture.named.worker_a
        assert isinstance(role, NodeRef)
        yield _Harness(
            fixture=fixture,
            backend=fixture.backend,
            path=fixture.path,
            role=role,
            role_id=role.node_id,
            parallel=True,
        )
    finally:
        fixture.doCleanups()


def _publish_matching(harness: _Harness) -> None:
    state = native.runtime_read_state(harness.path)
    assignment = state["roles"][harness.role_id]
    with mock.patch.object(native, "_assert_publisher"):
        native.publish_completion(
            harness.path,
            role=harness.role_id,
            run_id=state["run_id"],
            task_id=assignment["task_id"],
            dispatch_id=assignment["dispatch_id"],
            terminal_handle=assignment["terminal_handle"],
            launch_nonce=assignment["launch_nonce"],
            outcome="succeeded",
            body="published during exit detection",
            cleanup_confirmed=True,
        )


def _result_present(harness: _Harness) -> bool:
    state = native.runtime_read_state(harness.path)
    if harness.parallel:
        return "native_result" in state["roles"][harness.role_id]
    return "native_result" in state


def _wait(harness: _Harness) -> WaitReceipt:
    if harness.parallel:
        with harness.fixture.owner():
            result = harness.backend.request(RoleWait(harness.role, 1_000))
    else:
        result = harness.backend.request(RoleWait(harness.role, 1_000))
    assert isinstance(result, WaitReceipt)
    return result


class NativeWaitPublicationTest(unittest.TestCase):
    def test_exit_detection_rechecks_matching_completion(self) -> None:
        variants = (
            ("legacy", "cached_poll", _legacy_harness),
            ("legacy", "saved_status", _legacy_harness),
            ("parallel", "cached_poll", _parallel_harness),
            ("parallel", "saved_status", _parallel_harness),
        )
        for family, exit_path, make_harness in variants:
            for publish in (True, False):
                with (
                    self.subTest(family=family, exit_path=exit_path, publish=publish),
                    make_harness() as harness,
                ):
                    before = harness.path.read_bytes()
                    runner = harness.backend._runners[harness.role_id]
                    if exit_path == "cached_poll":
                        original_poll = runner.poll

                        def poll_after_publish(
                            original_poll=original_poll,
                            harness=harness,
                            publish=publish,
                        ):
                            if publish:
                                _publish_matching(harness)
                            return original_poll()

                        poll = mock.patch.object(
                            runner, "poll", side_effect=poll_after_publish
                        )
                        status = mock.patch.object(
                            harness.backend,
                            "_saved_runner_status",
                            side_effect=AssertionError(
                                "saved status path used for cached runner"
                            ),
                        )
                    else:
                        harness.backend._runners.pop(harness.role_id)

                        def saved_status_after_publish(
                            _assignment: object,
                            harness=harness,
                            publish=publish,
                        ) -> str:
                            if publish:
                                _publish_matching(harness)
                            return "exited"

                        poll = mock.patch.object(
                            runner,
                            "poll",
                            side_effect=AssertionError(
                                "cached poll path used for saved status"
                            ),
                        )
                        status = mock.patch.object(
                            harness.backend,
                            "_saved_runner_status",
                            side_effect=saved_status_after_publish,
                        )
                    try:
                        with poll, status:
                            if publish:
                                receipt = _wait(harness)
                                self.assertEqual(len(receipt.events), 1)
                                event = receipt.events[0]
                                self.assertEqual(event.kind, EventKind.WORKER_DONE)
                                self.assertEqual(event.outcome, Outcome.SUCCEEDED)
                                self.assertEqual(
                                    event.body, "published during exit detection"
                                )
                                self.assertIsNotNone(receipt.delivery_id)
                                self.assertTrue(_result_present(harness))
                            else:
                                with self.assertRaises(RuntimeFailure) as caught:
                                    _wait(harness)
                                self.assertIs(
                                    caught.exception.code,
                                    ErrorCode.BACKEND_PROTOCOL_FAILURE,
                                )
                                self.assertEqual(
                                    str(caught.exception),
                                    "native ACP runner exited without publishing completion",
                                )
                                self.assertFalse(_result_present(harness))
                                self.assertEqual(harness.path.read_bytes(), before)
                    finally:
                        if exit_path == "saved_status":
                            harness.backend._runners[harness.role_id] = runner


if __name__ == "__main__":
    unittest.main()
