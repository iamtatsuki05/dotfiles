"""Contract tests for the private durable workflow-effect adapter."""

from __future__ import annotations

import dataclasses
import importlib.util
import pickle
import unittest
from pathlib import Path
from typing import cast

from agent_team import workflow_effect_adapter as adapter
from agent_team.backend import OrcaBackend
from agent_team.contracts import (
    Attach,
    BackendPort,
    DeliveryAck,
    DeliveryRef,
    MessageRef,
    MessageReply,
    Role,
    RoleGet,
    RolePrompt,
    RoleRead,
    RoleRelease,
    RoleSpec,
    RoleWait,
    RuntimeRequest,
    StartSpec,
    Status,
    TeamRuntime,
)
from agent_team.workflow_store import OperationAction

_CAPABILITY_FIELDS = (
    "effect_key_idempotency",
    "pure_effect_lookup",
    "attempt_fence_enforcement",
    "consumer_generation",
    "exact_delivery_lookup",
    "exact_read_lookup",
    "composite_stop",
)
_RAW_CANARIES = (
    "raw-body-canary",
    "raw-output-canary",
    "argv-canary",
    "secret-canary",
    "path-canary",
    "instructions-canary",
)


class _BackendSpy:
    def __init__(self, capabilities: object = None) -> None:
        self.capabilities = capabilities
        self.calls: list[str] = []

    def durability_capabilities(self) -> object:
        self.calls.append("durability_capabilities")
        if isinstance(self.capabilities, BaseException):
            raise self.capabilities
        return self.capabilities

    def execute(self, *args: object, **kwargs: object) -> None:
        self.calls.append("execute")

    def lookup(self, *args: object, **kwargs: object) -> None:
        self.calls.append("lookup")


class _PublicBackendSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self, *args: object, **kwargs: object) -> None:
        self.calls.append("start")

    def request(self, *args: object, **kwargs: object) -> None:
        self.calls.append("request")

    def stop(self, *args: object, **kwargs: object) -> None:
        self.calls.append("stop")


def _all_true_capabilities() -> adapter.DurableEffectCapabilities:
    return adapter.DurableEffectCapabilities(
        version=1,
        effect_key_idempotency=True,
        pure_effect_lookup=True,
        attempt_fence_enforcement=True,
        consumer_generation=True,
        exact_delivery_lookup=True,
        exact_read_lookup=True,
        composite_stop=True,
    )


def _common_capabilities() -> adapter.DurableEffectCapabilities:
    return dataclasses.replace(
        _all_true_capabilities(),
        exact_delivery_lookup=False,
        exact_read_lookup=False,
        composite_stop=False,
    )


def _start_spec() -> StartSpec:
    return StartSpec(
        team_id="team-1",
        workspace=Path("/tmp/path-canary-workspace"),
        config_path=Path("/tmp/path-canary-config.toml"),
        state_path=Path("/tmp/path-canary-state"),
        role_specs={
            Role.WORKER: RoleSpec(
                provider="provider-1",
                transport="transport-1",
                model="model-1",
                effort="medium",
                permission="default",
                instructions="instructions-canary",
                execution="background",
            )
        },
    )


def _command_fields(command: object) -> dict[str, object]:
    if not dataclasses.is_dataclass(command):
        raise AssertionError("EffectCommand must be a dataclass value")
    return {
        field.name: getattr(command, field.name)
        for field in dataclasses.fields(command)
    }


def _invalid_operation_action(value: object) -> OperationAction:
    """Construct an intentionally invalid action for runtime rejection tests."""
    return cast(OperationAction, value)


def _invalid_role_wait(timeout_ms: object) -> RoleWait:
    """Construct an intentionally invalid wait request for runtime checks."""
    return RoleWait(Role.WORKER, cast(int, timeout_ms))


def _invalid_role_read(lines: object) -> RoleRead:
    """Construct an intentionally invalid read request for runtime checks."""
    return RoleRead(Role.WORKER, cast(int, lines))


def _invalid_capability_field(
    capabilities: adapter.DurableEffectCapabilities,
    field: str,
    value: object,
) -> adapter.DurableEffectCapabilities:
    """Construct an intentionally malformed capability value."""
    return dataclasses.replace(capabilities, **{field: cast(bool, value)})


def _assert_no_raw_canaries(test: unittest.TestCase, command: object) -> None:
    representations = [repr(command), str(command)]
    try:
        representations.append(pickle.dumps(command).decode("latin1"))
    except (TypeError, pickle.PicklingError):
        pass
    try:
        representations.append(
            repr(dataclasses.asdict(cast(adapter.EffectCommand, command)))
        )
    except (TypeError, dataclasses.FrozenInstanceError):
        pass
    for representation in representations:
        for canary in _RAW_CANARIES:
            test.assertNotIn(canary, representation)


def _assert_safe_field_values(
    test: unittest.TestCase, fields: dict[str, object]
) -> None:
    for name, value in fields.items():
        if name == "action":
            test.assertIsInstance(value, OperationAction)
        elif value is None:
            continue
        elif name == "role":
            test.assertIsInstance(value, Role)
        else:
            test.assertIsInstance(value, (str, int))
            test.assertFalse(isinstance(value, bool), name)


class WorkflowEffectContractTests(unittest.TestCase):
    def test_private_adapter_module_exists_without_widening_public_ports(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("agent_team.workflow_effect_adapter")
        )
        for protocol in (TeamRuntime, BackendPort):
            methods = {
                name
                for name, value in protocol.__dict__.items()
                if callable(value) and not name.startswith("_")
            }
            self.assertEqual({"start", "request", "stop"}, methods)

    def test_capability_gate_requires_exact_version_types_and_all_effect_guards(
        self,
    ) -> None:
        capabilities = _all_true_capabilities()
        self.assertTrue(dataclasses.is_dataclass(capabilities))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            capabilities.version = 2  # type: ignore[misc]
        backend = _BackendSpy(capabilities)
        self.assertIs(capabilities, adapter.require_durable_capabilities(backend))
        self.assertEqual(["durability_capabilities"], backend.calls)

        invalid_cases: list[tuple[str, object]] = [
            ("version-zero", dataclasses.replace(capabilities, version=0)),
            ("version-two", dataclasses.replace(capabilities, version=2)),
            ("version-bool", dataclasses.replace(capabilities, version=True)),
        ]
        for field in _CAPABILITY_FIELDS:
            invalid_cases.append(
                (
                    f"{field}-non-bool",
                    _invalid_capability_field(capabilities, field, 1),
                )
            )
        for field in (
            "attempt_fence_enforcement",
            "consumer_generation",
        ):
            invalid_cases.append(
                (f"{field}-false", dataclasses.replace(capabilities, **{field: False}))
            )
        invalid_cases.append(
            (
                "both-idempotency-and-lookup-false",
                dataclasses.replace(
                    capabilities,
                    effect_key_idempotency=False,
                    pure_effect_lookup=False,
                ),
            )
        )

        class CapabilitySubclass(adapter.DurableEffectCapabilities):
            pass

        invalid_cases.append(
            (
                "capability-subclass",
                CapabilitySubclass(
                    version=1,
                    effect_key_idempotency=True,
                    pure_effect_lookup=True,
                    attempt_fence_enforcement=True,
                    consumer_generation=True,
                    exact_delivery_lookup=True,
                    exact_read_lookup=True,
                    composite_stop=True,
                ),
            )
        )
        for name, invalid in invalid_cases:
            with self.subTest(name=name):
                invalid_backend = _BackendSpy(invalid)
                with self.assertRaises(adapter.DurabilityUnsupported):
                    adapter.require_durable_capabilities(invalid_backend)
                self.assertEqual(["durability_capabilities"], invalid_backend.calls)

        class MissingCapability:
            pass

        missing_backend = _BackendSpy(MissingCapability())
        with self.assertRaises(adapter.DurabilityUnsupported):
            adapter.require_durable_capabilities(missing_backend)
        self.assertEqual(["durability_capabilities"], missing_backend.calls)

        flapping = _BackendSpy(capabilities)

        def raise_capabilities() -> object:
            raise RuntimeError("secret-canary")

        object.__setattr__(flapping, "durability_capabilities", raise_capabilities)
        with self.assertRaises(adapter.DurabilityUnsupported) as raised:
            adapter.require_durable_capabilities(flapping)
        self.assertNotIn("secret-canary", str(raised.exception))
        self.assertEqual([], flapping.calls)

        common = _common_capabilities()
        common_backend = _BackendSpy(common)
        self.assertIs(common, adapter.require_durable_capabilities(common_backend))
        self.assertEqual(["durability_capabilities"], common_backend.calls)

    def test_action_capability_gate_requires_only_the_effect_specific_proof(
        self,
    ) -> None:
        common = _common_capabilities()
        for action in (
            OperationAction.START,
            OperationAction.PROMPT,
            OperationAction.REPLY,
            OperationAction.RELEASE,
            OperationAction.ACK,
        ):
            with self.subTest(action=action.value):
                adapter.require_durable_action(common, action)

        requirements = (
            (OperationAction.WAIT, "exact_delivery_lookup"),
            (OperationAction.READ, "exact_read_lookup"),
            (OperationAction.STOP, "composite_stop"),
        )
        for action, field in requirements:
            with self.subTest(action=action.value):
                with self.assertRaises(adapter.DurabilityUnsupported):
                    adapter.require_durable_action(common, action)
                adapter.require_durable_action(
                    dataclasses.replace(common, **{field: True}),
                    action,
                )
        stop_without_lookup = dataclasses.replace(
            common,
            effect_key_idempotency=True,
            pure_effect_lookup=False,
            composite_stop=True,
        )
        adapter.require_durable_action(
            stop_without_lookup,
            OperationAction.START,
        )
        with self.assertRaises(adapter.DurabilityUnsupported):
            adapter.require_durable_action(
                stop_without_lookup,
                OperationAction.STOP,
            )
        with self.assertRaises(adapter.DurabilityUnsupported):
            adapter.require_durable_action(common, _invalid_operation_action("wait"))

    def test_capability_gate_requires_callable_execute_and_lookup_without_effect_calls(
        self,
    ) -> None:
        capabilities = _all_true_capabilities()

        no_execute = _BackendSpy(capabilities)
        object.__setattr__(no_execute, "execute", None)
        no_lookup = _BackendSpy(capabilities)
        object.__setattr__(no_lookup, "lookup", None)

        for backend in (no_execute, no_lookup):
            with (
                self.subTest(type=type(backend).__name__),
                self.assertRaises(adapter.DurabilityUnsupported),
            ):
                adapter.require_durable_capabilities(backend)
            self.assertEqual(["durability_capabilities"], backend.calls)

        class RaisingEffectPort:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def durability_capabilities(self) -> object:
                self.calls.append("durability_capabilities")
                return capabilities

            @property
            def execute(self) -> object:
                raise RuntimeError("secret-canary")

            def lookup(self, *args: object, **kwargs: object) -> None:
                del args, kwargs
                self.calls.append("lookup")

        raising = RaisingEffectPort()
        with self.assertRaises(adapter.DurabilityUnsupported) as raised:
            adapter.require_durable_capabilities(raising)
        self.assertNotIn("secret-canary", str(raised.exception))
        self.assertEqual(["durability_capabilities"], raising.calls)

    def test_public_backend_and_orca_like_objects_are_rejected_before_effects(
        self,
    ) -> None:
        backend = _PublicBackendSpy()
        with self.assertRaises(adapter.DurabilityUnsupported) as raised:
            adapter.require_durable_capabilities(backend)
        self.assertNotIn("argv-canary", str(raised.exception))
        self.assertNotIn("secret-canary", str(raised.exception))
        self.assertEqual([], backend.calls)

        class OrcaLike(_PublicBackendSpy):
            argv = ("orca", "argv-canary")
            prompt = "raw-body-canary"
            secret = "secret-canary"

        orca_like = OrcaLike()
        with self.assertRaises(adapter.DurabilityUnsupported) as raised:
            adapter.require_durable_capabilities(orca_like)
        self.assertNotIn("argv-canary", str(raised.exception))
        self.assertNotIn("raw-body-canary", str(raised.exception))
        self.assertNotIn("secret-canary", str(raised.exception))
        self.assertEqual([], orca_like.calls)

        class ClientProbe:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def __getattr__(self, name: str) -> object:
                self.calls.append(name)
                raise AssertionError("current Orca client must not be consulted")

        client = ClientProbe()
        current_orca = OrcaBackend(client)  # type: ignore[arg-type]
        with self.assertRaises(adapter.DurabilityUnsupported):
            adapter.require_durable_capabilities(current_orca)
        self.assertEqual([], client.calls)

    def test_effect_command_maps_exactly_eight_actions_to_safe_typed_values(
        self,
    ) -> None:
        self.assertTrue(dataclasses.is_dataclass(adapter.EffectCommand))
        request_cases: tuple[tuple[str, RuntimeRequest, OperationAction], ...] = (
            (
                "prompt",
                RolePrompt(Role.WORKER, "raw-body-canary"),
                OperationAction.PROMPT,
            ),
            ("wait", RoleWait(Role.WORKER, 250), OperationAction.WAIT),
            ("read", RoleRead(Role.WORKER, 8), OperationAction.READ),
            ("release", RoleRelease(Role.WORKER), OperationAction.RELEASE),
            (
                "reply",
                MessageReply(MessageRef("message-1"), "raw-output-canary"),
                OperationAction.REPLY,
            ),
            (
                "ack",
                DeliveryAck(DeliveryRef("delivery-1")),
                OperationAction.ACK,
            ),
        )
        commands = [
            ("start", adapter.make_start_command(_start_spec()), OperationAction.START),
            *(
                (name, adapter.make_request_command(request), action)
                for name, request, action in request_cases
            ),
            ("stop", adapter.make_stop_command(), OperationAction.STOP),
        ]
        self.assertEqual(8, len(commands))
        for name, command, expected_action in commands:
            with self.subTest(name=name):
                fields = _command_fields(command)
                self.assertEqual(expected_action, fields["action"])
                _assert_safe_field_values(self, fields)
                self.assertTrue(
                    all(
                        field == "action"
                        or field.endswith("_digest")
                        or field
                        in {
                            "role",
                            "timeout_ms",
                            "lines",
                            "message_id",
                            "delivery_id",
                            "team_id",
                            "root_key",
                        }
                        for field in fields
                    ),
                    fields,
                )
                self.assertFalse(hasattr(command, "__dict__"))
                self.assertTrue(
                    any(name.endswith("digest") for name in fields),
                    fields,
                )
                _assert_no_raw_canaries(self, command)

        prompt = adapter.make_request_command(
            RolePrompt(Role.WORKER, "raw-body-canary")
        )
        prompt_fields = _command_fields(prompt)
        self.assertEqual(Role.WORKER, prompt_fields["role"])
        prompt_digest = prompt_fields.get(
            "parameter_digest", prompt_fields.get("request_digest")
        )
        assert isinstance(prompt_digest, str)
        self.assertNotIn("raw-body-canary", prompt_digest)

        wait = adapter.make_request_command(RoleWait(Role.WORKER, 250))
        self.assertEqual(250, _command_fields(wait)["timeout_ms"])
        read = adapter.make_request_command(RoleRead(Role.WORKER, 8))
        self.assertEqual(8, _command_fields(read)["lines"])
        release = adapter.make_request_command(RoleRelease(Role.WORKER))
        self.assertEqual(Role.WORKER, _command_fields(release)["role"])
        reply = adapter.make_request_command(
            MessageReply(MessageRef("message-1"), "raw-output-canary")
        )
        self.assertEqual("message-1", _command_fields(reply)["message_id"])
        ack = adapter.make_request_command(DeliveryAck(DeliveryRef("delivery-1")))
        self.assertEqual("delivery-1", _command_fields(ack)["delivery_id"])

    def test_effect_command_rejects_non_effect_requests_and_invalid_bounds(
        self,
    ) -> None:
        for non_effect_request in (
            Status(),
            Attach(Role.WORKER),
            RoleGet(Role.WORKER),
        ):
            with (
                self.subTest(request=type(non_effect_request).__name__),
                self.assertRaises((TypeError, ValueError)),
            ):
                adapter.make_request_command(non_effect_request)

        accepted = "a" * 1_048_576
        command = adapter.make_request_command(RolePrompt(Role.WORKER, accepted))
        self.assertEqual(OperationAction.PROMPT, _command_fields(command)["action"])
        _assert_no_raw_canaries(self, command)
        utf8_accepted = "あ" * 349_525 + "a"
        self.assertEqual(1_048_576, len(utf8_accepted.encode("utf-8")))
        adapter.make_request_command(RolePrompt(Role.WORKER, utf8_accepted))
        with self.assertRaises(ValueError):
            adapter.make_request_command(RolePrompt(Role.WORKER, accepted + "a"))
        utf8_oversized = "raw-body-canary" + "a" * (1_048_577 - len("raw-body-canary"))
        self.assertEqual(1_048_577, len(utf8_oversized.encode("utf-8")))
        with self.assertRaises(ValueError) as raised:
            adapter.make_request_command(RolePrompt(Role.WORKER, utf8_oversized))
        self.assertNotIn("raw-body-canary", str(raised.exception))
        with self.assertRaises(ValueError):
            adapter.make_request_command(RolePrompt(Role.WORKER, " \t\n"))
        with self.assertRaises(ValueError):
            adapter.make_request_command(MessageReply(MessageRef("message-1"), ""))
        with self.assertRaises(adapter.DurabilityUnsupported):
            adapter.make_start_command(dataclasses.replace(_start_spec(), attach=True))

        invalid_requests: tuple[RuntimeRequest, ...] = (
            RoleWait(Role.WORKER, 0),
            RoleWait(Role.WORKER, -1),
            RoleWait(Role.WORKER, True),
            _invalid_role_wait(1.5),
            RoleRead(Role.WORKER, 0),
            RoleRead(Role.WORKER, -1),
            RoleRead(Role.WORKER, False),
            _invalid_role_read(1.5),
        )
        for request in invalid_requests:
            with (
                self.subTest(request=request),
                self.assertRaises((TypeError, ValueError)),
            ):
                adapter.make_request_command(request)


if __name__ == "__main__":
    unittest.main()
