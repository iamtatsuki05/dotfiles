"""Contract tests for the pure workflow checkpoint value/codec boundary."""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import pickle
import unittest
from collections.abc import Callable
from typing import Any

from agent_team import workflow_store as workflow_store_module


class WorkflowStoreContractTest(unittest.TestCase):
    """Pure value, codec, digest, and public-shape contract tests."""

    def test_workflow_store_module_is_available(self) -> None:
        spec = importlib.util.find_spec("agent_team.workflow_store")
        self.assertIsNotNone(spec, "workflow_store contract module is missing")
        for name in (
            "STORE_SCHEMA",
            "REQUEST_DIGEST_DOMAIN",
            "DELIVERY_DIGEST_DOMAIN",
            "WAIT_TIMEOUT_DIGEST_DOMAIN",
            "delivery_content_digest",
            "wait_timeout_digest",
        ):
            self.assertIn(name, workflow_store_module.__all__)

    @staticmethod
    def _module() -> Any:
        return workflow_store_module

    def _root(self) -> Any:
        module = self._module()
        return module.RootIdentity(
            root_key="root-1",
            team_id="team-1",
            workspace=module.PathIdentity(
                path="/tmp/agent-team-workspace",
                device=10,
                inode=11,
            ),
            config_path="/tmp/agent-team-config.toml",
            config_device=10,
            config_inode=12,
            config_digest="sha256:" + "1" * 64,
            state_root=module.PathIdentity(
                path="/tmp/agent-team-state",
                device=10,
                inode=13,
            ),
        )

    def _draft(self) -> Any:
        module = self._module()
        root = self._root()
        run = module.RunIdentity(
            run_id="run-1", main_terminal_id="terminal-main", consumer_generation=1
        )
        completion = module.CompletionIdentity(
            run_id="run-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            sender_terminal_id="terminal-worker",
        )
        assignment = module.ActiveAssignment(
            role=module.AssignmentRole.WORKER,
            worker_node="worker-node-1",
            task_id="task-1",
            attempt=1,
            dispatch_id="dispatch-1",
            terminal_id="terminal-worker",
            launch_mode=module.LaunchMode.BARE_BACKGROUND,
            completion_identity=completion,
        )
        projection = module.EventProjection(
            kind=module.EventProjectionKind.WORKER_DONE,
            message_id=None,
            completion_identity=completion,
            outcome=module.EventOutcome.SUCCEEDED,
            body_digest="sha256:" + "2" * 64,
        )
        delivery = module.PendingDelivery(
            delivery_id="delivery-1",
            consumer_generation=1,
            ordered_message_ids=(),
            ordered_event_projection=(projection,),
            delivery_digest=module.delivery_content_digest(
                delivery_id="delivery-1",
                consumer_generation=1,
                ordered_message_ids=(),
                ordered_event_projection=(projection,),
            ),
            ack_operation_id=None,
            ack_status=module.AckStatus.PENDING,
        )
        policy = module.TaskPolicyReference(
            version=4,
            team_id="team-1",
            workspace="/tmp/agent-team-workspace",
            task_id="task-1",
            sequence=1,
            state_digest="sha256:" + "4" * 64,
        )
        return module.WorkflowCheckpointDraft(
            root=root,
            run=run,
            workflow_sequence=2,
            task_sequence=1,
            execution_mode=module.ExecutionMode.SERIAL,
            workflow_state=module.CheckpointState.WORKER_DONE,
            task_policy=policy,
            active_assignment=assignment,
            pending_delivery=delivery,
            replied_message_ids=(),
            read_observed=False,
            released=False,
            review_authority=module.AuthorityReference(
                reference="review-ref-1", digest="sha256:" + "5" * 64
            ),
            verification_authority=None,
            last_operation=module.LastOperation(
                operation_id="operation-1",
                effect_key="effect-1",
                action=module.OperationAction.WAIT,
                request_digest="sha256:" + "6" * 64,
                expected_workflow_sequence=1,
                expected_task_sequence=0,
                status=module.OperationStatus.COMMITTED,
                receipt_id="receipt-1",
                receipt_digest="sha256:" + "7" * 64,
            ),
        )

    def test_checkpoint_fixture_has_stable_canonical_bytes_and_digest(self) -> None:
        module = self._module()
        checkpoint = module._issue_checkpoint(
            self._draft(), updated_ns=123, issuer=object()
        )
        encoded = module.encode_checkpoint(checkpoint)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded, module.encode_checkpoint(checkpoint))
        decoded = module.decode_checkpoint(encoded)
        self.assertEqual(decoded, checkpoint)
        self.assertEqual(
            checkpoint.checkpoint_digest,
            module.compute_checkpoint_digest(encoded),
        )
        self.assertEqual(
            tuple(json.loads(encoded.decode("utf-8"))),
            module.CHECKPOINT_FIELDS,
        )
        self.assertEqual(
            module.checkpoint_scalar_projection(checkpoint)["workflow_sequence"], 2
        )

    def test_full_checkpoint_requires_committed_start_sequence(self) -> None:
        for sequence in (0, 1):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                dataclasses.replace(self._draft(), workflow_sequence=sequence)

    def test_checkpoint_digest_omits_its_own_field_and_uses_v4_domain(self) -> None:
        module = self._module()
        checkpoint = module._issue_checkpoint(
            self._draft(), updated_ns=123, issuer=object()
        )
        mapping = module.checkpoint_mapping(checkpoint)
        digest = mapping.pop("checkpoint_digest")
        body = module._canonical_json(mapping)
        expected = module._domain_digest(module.CHECKPOINT_DIGEST_DOMAIN, body)
        self.assertEqual(digest, expected)
        self.assertNotEqual(
            digest,
            module._domain_digest(
                module.CHECKPOINT_DIGEST_DOMAIN, body + digest.encode()
            ),
        )
        self.assertTrue(module.CHECKPOINT_DIGEST_DOMAIN.endswith(b"\0"))

    def test_checkpoint_codec_rejects_noncanonical_wire_values(self) -> None:
        module = self._module()
        checkpoint = module._issue_checkpoint(
            self._draft(), updated_ns=123, issuer=object()
        )
        encoded = module.encode_checkpoint(checkpoint)
        cases: list[tuple[str, bytes]] = [
            ("bom", b"\xef\xbb\xbf" + encoded),
            ("trailing-space", encoded[:-1] + b" \n"),
            ("trailing-data", encoded + b"{}"),
            ("reordered", encoded.replace(b'"store_schema":4', b'"store_schema":4,')),
            ("unicode-escape", encoded.replace(b"root-1", b"root-\\u0031")),
            (
                "duplicate",
                encoded.replace(
                    b'"checkpoint_version":4',
                    b'"checkpoint_version":4,"checkpoint_version":4',
                ),
            ),
        ]
        for name, raw in cases:
            with (
                self.subTest(name=name),
                self.assertRaises(module.CheckpointSchemaError),
            ):
                module.decode_checkpoint(raw)

        mapping = module.checkpoint_mapping(checkpoint)
        mutations: tuple[tuple[str, Callable[[dict[str, object]], object]], ...] = (
            ("missing", lambda value: value.pop("workflow_state")),
            ("unknown", lambda value: value.__setitem__("future", None)),
            ("float", lambda value: value.__setitem__("workflow_sequence", 2.0)),
            ("bool", lambda value: value.__setitem__("workflow_sequence", True)),
            (
                "digest",
                lambda value: value.__setitem__(
                    "checkpoint_digest", "sha256:" + "0" * 64
                ),
            ),
        )
        for name, mutate in mutations:
            changed = dict(mapping)
            mutate(changed)
            raw = module._canonical_json(changed)
            with (
                self.subTest(name=name),
                self.assertRaises(module.CheckpointSchemaError),
            ):
                module.decode_checkpoint(raw)

    def test_wire_parser_firewalls_input_details_and_recursion_errors(self) -> None:
        module = self._module()
        canary = b"input-secret-canary"
        malformed = b'{"input":"' + canary + b'"'
        deeply_nested = b"[" * 10_000 + b"]" * 10_000
        cases = (
            (
                "checkpoint malformed",
                module.decode_checkpoint,
                malformed,
                module.CheckpointSchemaError,
            ),
            ("seed malformed", module.decode_seed, malformed, module.SeedSchemaError),
            (
                "checkpoint deeply nested",
                module.decode_checkpoint,
                deeply_nested,
                module.CheckpointSchemaError,
            ),
            (
                "seed deeply nested",
                module.decode_seed,
                deeply_nested,
                module.SeedSchemaError,
            ),
        )
        for name, decoder, raw, error_type in cases:
            with self.subTest(name=name), self.assertRaises(error_type) as raised:
                decoder(raw)
            error = raised.exception
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertNotIn(canary.decode(), str(error))

    def test_seed_has_a_separate_strict_codec_and_digest(self) -> None:
        module = self._module()
        seed = module.WorkflowRootSeed(root=self._root())
        encoded = module.encode_seed(seed)
        decoded = module.decode_seed(encoded)
        self.assertEqual(decoded, seed)
        self.assertEqual(tuple(json.loads(encoded.decode("utf-8"))), module.SEED_FIELDS)
        with self.assertRaises(module.CheckpointSchemaError):
            module.decode_checkpoint(encoded)
        with self.assertRaises(module.SeedSchemaError):
            module.decode_seed(
                encoded.replace(b'"seed_version":1', b'"seed_version":2')
            )

        operation = module.WorkflowRootSeed(
            root=self._root(),
            workflow_sequence=1,
            operation_id="operation-1",
            operation_status=module.OperationStatus.INTENT,
        )
        self.assertEqual(module.decode_seed(module.encode_seed(operation)), operation)
        unknown = module.WorkflowRootSeed(
            root=self._root(),
            workflow_sequence=2,
            operation_id="operation-1",
            operation_status=module.OperationStatus.UNKNOWN_EFFECT,
        )
        self.assertEqual(module.decode_seed(module.encode_seed(unknown)), unknown)

    def test_schema3_codec_is_explicit_and_isolated_from_current_encoder(self) -> None:
        """A v3 observation may only cross the private migration boundary."""

        module = self._module()
        checkpoint = module._issue_checkpoint(
            self._draft(), updated_ns=123, issuer=object()
        )
        checkpoint_mapping = module.checkpoint_mapping(checkpoint)
        checkpoint_mapping["store_schema"] = 3
        checkpoint_body = dict(checkpoint_mapping)
        del checkpoint_body["checkpoint_digest"]
        checkpoint_mapping["checkpoint_digest"] = module._domain_digest(
            module.CHECKPOINT_DIGEST_DOMAIN,
            module._canonical_json(checkpoint_body),
        )
        raw_checkpoint = module._canonical_json(checkpoint_mapping)

        seed = module.WorkflowRootSeed(root=self._root())
        seed_mapping = dict(json.loads(module.encode_seed(seed).decode("utf-8")))
        seed_mapping["store_schema"] = 3
        seed_body = dict(seed_mapping)
        del seed_body["seed_digest"]
        seed_mapping["seed_digest"] = module._domain_digest(
            module.SEED_DIGEST_DOMAIN,
            module._canonical_json(seed_body),
        )
        raw_seed = module._canonical_json(seed_mapping)

        with self.assertRaises(module.CheckpointSchemaError):
            module.decode_checkpoint(raw_checkpoint)
        with self.assertRaises(module.SeedSchemaError):
            module.decode_seed(raw_seed)

        legacy_checkpoint = module.decode_checkpoint(
            raw_checkpoint, expected_store_schema=3
        )
        legacy_seed = module.decode_seed(raw_seed, expected_store_schema=3)
        self.assertEqual(3, legacy_checkpoint.store_schema)
        self.assertEqual(3, legacy_seed.store_schema)
        self.assertEqual(
            raw_checkpoint, module._encode_checkpoint_v3(legacy_checkpoint)
        )
        self.assertEqual(raw_seed, module._encode_seed_v3(legacy_seed))
        self.assertEqual(
            raw_checkpoint,
            module._encode_checkpoint_v3(module._decode_checkpoint_v3(raw_checkpoint)),
        )
        self.assertEqual(
            raw_seed,
            module._encode_seed_v3(module._decode_seed_v3(raw_seed)),
        )

        with self.assertRaises(module.CheckpointSchemaError):
            module.encode_checkpoint(legacy_checkpoint)
        with self.assertRaises(module.SeedSchemaError):
            module.encode_seed(legacy_seed)
        with self.assertRaises(module.CheckpointSchemaError):
            module.checkpoint_mapping(legacy_checkpoint)
        with self.assertRaises(module.CheckpointSchemaError):
            module.checkpoint_to_draft(legacy_checkpoint)
        with self.assertRaises(module.CheckpointSchemaError):
            module.checkpoint_scalar_projection(legacy_checkpoint)
        with self.assertRaises(module.SeedSchemaError):
            module.seed_scalar_projection(legacy_seed)
        with self.assertRaises(ValueError):
            module._issue_checkpoint(
                self._draft(), updated_ns=123, issuer=object(), store_schema=3
            )

    def test_schema3_decoder_uses_frozen_codec_values(self) -> None:
        """Changing target-v4 globals cannot alter migration-only v3 decoding."""

        module = self._module()
        checkpoint = module._issue_checkpoint(
            self._draft(), updated_ns=123, issuer=object()
        )
        checkpoint_mapping = module.checkpoint_mapping(checkpoint)
        checkpoint_mapping["store_schema"] = 3
        checkpoint_body = dict(checkpoint_mapping)
        del checkpoint_body["checkpoint_digest"]
        checkpoint_mapping["checkpoint_digest"] = module._domain_digest(
            module.CHECKPOINT_DIGEST_DOMAIN,
            module._canonical_json(checkpoint_body),
        )
        raw_checkpoint = module._canonical_json(checkpoint_mapping)

        seed = module.WorkflowRootSeed(root=self._root())
        seed_mapping = dict(json.loads(module.encode_seed(seed).decode("utf-8")))
        seed_mapping["store_schema"] = 3
        seed_body = dict(seed_mapping)
        del seed_body["seed_digest"]
        seed_mapping["seed_digest"] = module._domain_digest(
            module.SEED_DIGEST_DOMAIN,
            module._canonical_json(seed_body),
        )
        raw_seed = module._canonical_json(seed_mapping)

        original_values = (
            module.CHECKPOINT_VERSION,
            module.SEED_VERSION,
            module.CHECKPOINT_FIELDS,
            module.SEED_FIELDS,
            module.WORKFLOW_EVENT_SCHEMA_VERSION,
        )
        try:
            module.CHECKPOINT_VERSION = 99
            module.SEED_VERSION = 99
            module.CHECKPOINT_FIELDS = ("future",)
            module.SEED_FIELDS = ("future",)
            module.WORKFLOW_EVENT_SCHEMA_VERSION = 99
            decoded_checkpoint = module.decode_checkpoint(
                raw_checkpoint, expected_store_schema=3
            )
            decoded_seed = module.decode_seed(raw_seed, expected_store_schema=3)
        finally:
            (
                module.CHECKPOINT_VERSION,
                module.SEED_VERSION,
                module.CHECKPOINT_FIELDS,
                module.SEED_FIELDS,
                module.WORKFLOW_EVENT_SCHEMA_VERSION,
            ) = original_values

        self.assertEqual(3, decoded_checkpoint.store_schema)
        self.assertEqual(3, decoded_seed.store_schema)

    def test_legacy_seed_digest_and_projection_are_frozen(self) -> None:
        """A legacy seed keeps its raw digest despite target-domain rebinding."""

        module = self._module()
        seed = module.WorkflowRootSeed(root=self._root())
        seed_mapping = dict(json.loads(module.encode_seed(seed).decode("utf-8")))
        seed_mapping["store_schema"] = 3
        seed_body = dict(seed_mapping)
        del seed_body["seed_digest"]
        legacy_digest = module._domain_digest(
            b"agent-team/workflow-seed/v1\0",
            module._canonical_json(seed_body),
        )
        seed_mapping["seed_digest"] = legacy_digest
        raw_seed = module._canonical_json(seed_mapping)
        legacy_seed = module.decode_seed(raw_seed, expected_store_schema=3)

        original_domain = module.SEED_DIGEST_DOMAIN
        try:
            module.SEED_DIGEST_DOMAIN = b"future-seed-domain\0"
            self.assertEqual(legacy_digest, legacy_seed.seed_digest)
            projection = module._legacy_seed_scalar_projection(legacy_seed)
        finally:
            module.SEED_DIGEST_DOMAIN = original_domain

        self.assertEqual(legacy_digest, projection["checkpoint_digest"])
        self.assertEqual(raw_seed, projection["checkpoint_bytes"])

    def test_current_boundary_cannot_be_rebound_to_schema3(self) -> None:
        """Rebinding the public marker cannot open a v3 path in current APIs."""

        module = self._module()
        checkpoint = module._issue_checkpoint(
            self._draft(), updated_ns=123, issuer=object()
        )
        checkpoint_mapping = module.checkpoint_mapping(checkpoint)
        checkpoint_mapping["store_schema"] = 3
        checkpoint_body = dict(checkpoint_mapping)
        del checkpoint_body["checkpoint_digest"]
        checkpoint_mapping["checkpoint_digest"] = module._domain_digest(
            module.CHECKPOINT_DIGEST_DOMAIN,
            module._canonical_json(checkpoint_body),
        )
        legacy_checkpoint = module.decode_checkpoint(
            module._canonical_json(checkpoint_mapping), expected_store_schema=3
        )

        seed = module.WorkflowRootSeed(root=self._root())
        seed_mapping = dict(json.loads(module.encode_seed(seed).decode("utf-8")))
        seed_mapping["store_schema"] = 3
        seed_body = dict(seed_mapping)
        del seed_body["seed_digest"]
        seed_mapping["seed_digest"] = module._domain_digest(
            module.SEED_DIGEST_DOMAIN,
            module._canonical_json(seed_body),
        )
        legacy_seed = module.decode_seed(
            module._canonical_json(seed_mapping), expected_store_schema=3
        )

        original_schema = module.STORE_SCHEMA
        try:
            module.STORE_SCHEMA = 3
            with self.assertRaises(module.CheckpointSchemaError):
                module.encode_checkpoint(legacy_checkpoint)
            with self.assertRaises(module.SeedSchemaError):
                module.encode_seed(legacy_seed)
            with self.assertRaises(ValueError):
                module._issue_checkpoint(
                    self._draft(), updated_ns=123, issuer=object(), store_schema=3
                )
        finally:
            module.STORE_SCHEMA = original_schema

    def test_nested_values_enforce_order_identity_and_nullability(self) -> None:
        module = self._module()
        completion = module.CompletionIdentity(
            run_id="run-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            sender_terminal_id="terminal-worker",
        )
        projection = module.EventProjection(
            kind=module.EventProjectionKind.QUESTION,
            message_id="message-1",
            completion_identity=completion,
            outcome=None,
            body_digest="sha256:" + "2" * 64,
        )
        with self.assertRaises(ValueError):
            module.PendingDelivery(
                delivery_id="delivery-1",
                consumer_generation=1,
                ordered_message_ids=(),
                ordered_event_projection=(projection,),
                delivery_digest="sha256:" + "3" * 64,
                ack_operation_id=None,
                ack_status=module.AckStatus.PENDING,
            )

    def test_delivery_digest_binds_ordered_projection_content(self) -> None:
        module = self._module()
        completion = module.CompletionIdentity(
            run_id="run-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            sender_terminal_id="terminal-worker",
        )
        projection = module.EventProjection(
            kind=module.EventProjectionKind.QUESTION,
            message_id="message-1",
            completion_identity=completion,
            outcome=None,
            body_digest="sha256:" + "1" * 64,
        )
        digest = module.delivery_content_digest(
            delivery_id="delivery-1",
            consumer_generation=1,
            ordered_message_ids=("message-1",),
            ordered_event_projection=(projection,),
        )
        delivery = module.PendingDelivery(
            delivery_id="delivery-1",
            consumer_generation=1,
            ordered_message_ids=("message-1",),
            ordered_event_projection=(projection,),
            delivery_digest=digest,
            ack_operation_id=None,
            ack_status=module.AckStatus.PENDING,
        )
        self.assertEqual(digest, delivery.delivery_digest)
        with self.assertRaises(ValueError):
            dataclasses.replace(
                delivery,
                ordered_message_ids=("message-foreign",),
            )
        with self.assertRaises(ValueError):
            module.PendingDelivery(
                delivery_id="delivery-1",
                consumer_generation=1,
                ordered_message_ids=("message-1", "message-1"),
                ordered_event_projection=(projection, projection),
                delivery_digest="sha256:" + "3" * 64,
                ack_operation_id=None,
                ack_status=module.AckStatus.PENDING,
            )
        with self.assertRaises(ValueError):
            module.EventProjection(
                kind=module.EventProjectionKind.WORKER_DONE,
                message_id="message-1",
                completion_identity=completion,
                outcome=module.EventOutcome.SUCCEEDED,
                body_digest="sha256:" + "2" * 64,
            )
        with self.assertRaises(ValueError):
            module.PathIdentity(path="relative", device=1, inode=1)
        with self.assertRaises(ValueError):
            module.PathIdentity(path="/tmp/a\x00b", device=1, inode=1)
        with self.assertRaises(ValueError):
            module.RunIdentity(
                run_id="run", main_terminal_id="terminal", consumer_generation=True
            )

    def test_draft_and_store_issued_checkpoint_are_separate(self) -> None:
        module = self._module()
        draft = self._draft()
        self.assertFalse(hasattr(draft, "checkpoint_digest"))
        with self.assertRaises(TypeError):
            module.WorkflowCheckpointV4()
        checkpoint = module._issue_checkpoint(draft, updated_ns=99, issuer=object())
        self.assertIsInstance(checkpoint, module.WorkflowCheckpointV4)
        self.assertEqual(checkpoint.updated_ns, 99)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            checkpoint.updated_ns = 100

    def test_operation_intent_requires_stable_id_and_strict_identity(self) -> None:
        module = self._module()
        intent = module.OperationIntent(
            operation_id="operation-1",
            effect_key="effect-1",
            root_key="root-1",
            root=None,
            action=module.OperationAction.PROMPT,
            request_digest="sha256:" + "1" * 64,
            expected_workflow_sequence=2,
            expected_task_sequence=1,
            run_id="run-1",
            main_terminal_id="terminal-main",
            task_id="task-1",
            dispatch_id="dispatch-1",
            attempt=1,
            terminal_id="terminal-worker",
            delivery_id=None,
            message_id=None,
            consumer_generation=1,
            owner="owner-1",
            lease_epoch=1,
            fencing_token=1,
            actor="actor-1",
            evidence_ref=None,
        )
        self.assertEqual(intent.operation_id, "operation-1")
        with self.assertRaises(ValueError):
            module.OperationIntent(
                operation_id="operation-1",
                effect_key="effect-1",
                root_key="root-1",
                root=self._root(),
                action=module.OperationAction.START,
                request_digest="sha256:" + "1" * 64,
                expected_workflow_sequence=1,
                expected_task_sequence=None,
                run_id="synthetic-run",
                main_terminal_id="synthetic-main",
                task_id=None,
                dispatch_id=None,
                attempt=None,
                terminal_id=None,
                delivery_id=None,
                message_id=None,
                consumer_generation=0,
                owner="owner-1",
                lease_epoch=0,
                fencing_token=0,
                actor="actor-1",
                evidence_ref=None,
            )

    def test_store_authorities_are_exact_immutable_and_noncopyable(self) -> None:
        module = self._module()
        issuer = object()
        handle = module._issue_operation_handle(
            issuer=issuer,
            root_key="root-1",
            operation_id="operation-1",
            intent_sequence=3,
            owner="owner-1",
            lease_epoch=1,
            fencing_token=1,
        )
        receipt = module._issue_durable_receipt(
            issuer=issuer,
            receipt_id="receipt-1",
            operation_id="operation-1",
            effect_key="effect-1",
            action=module.OperationAction.PROMPT,
            request_digest="sha256:" + "1" * 64,
            root_key="root-1",
            run_id="run-1",
            main_terminal_id="terminal-main",
            task_id="task-1",
            dispatch_id="dispatch-1",
            attempt=1,
            terminal_id="terminal-worker",
            delivery_id=None,
            message_id=None,
            consumer_generation=1,
            owner="owner-1",
            lease_epoch=1,
            fencing_token=1,
            effect_ref="effect-ref-1",
            result_kind="start-result",
            result_digest="sha256:" + "2" * 64,
            evidence_ref="sha256:" + "3" * 64,
            issued_ns=10,
        )
        for value in (handle, receipt):
            self.assertIs(type(value), type(value))
            self.assertNotIn("secret", repr(value).lower())
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(TypeError):
                    copy.copy(value)
                with self.assertRaises(TypeError):
                    copy.deepcopy(value)
                with self.assertRaises(TypeError):
                    pickle.dumps(value)
        with self.assertRaises(TypeError):
            module.OperationHandle()
        with self.assertRaises(TypeError):
            module.DurableReceipt()
        self.assertIsNone(module._validate_operation_handle(handle, issuer=issuer))
        self.assertIsNone(module._validate_durable_receipt(receipt, issuer=issuer))
        with self.assertRaises(module.OperationIdentityConflict):
            module._validate_operation_handle(handle, issuer=object())

    def test_protocol_and_return_types_do_not_expose_sqlite_or_raw_body(self) -> None:
        module = self._module()
        expected_methods = {
            "load_checkpoint",
            "begin_operation",
            "commit_effect",
            "commit_transition",
            "lookup_operation",
            "mark_unknown",
        }
        self.assertTrue(expected_methods <= set(module.WorkflowStorePort.__dict__))
        self.assertFalse(any("sqlite" in name.lower() for name in dir(module)))
        self.assertNotIn("body", module.DurableReceipt.__slots__)
        self.assertNotIn("response", module.DurableReceipt.__slots__)

    def test_seed_projection_binds_clock_and_all_scalar_columns(self) -> None:
        module = self._module()
        seed = module.WorkflowRootSeed(root=self._root(), updated_ns=17)
        encoded = module.encode_seed(seed)
        decoded = module.decode_seed(encoded)
        self.assertEqual(17, decoded.updated_ns)
        projection = module.seed_scalar_projection(decoded)
        self.assertEqual(17, projection["updated_ns"])
        self.assertEqual(encoded, projection["checkpoint_bytes"])
        self.assertEqual(decoded.seed_digest, projection["checkpoint_digest"])

    def test_reply_order_preserves_actual_durable_mutation_order(self) -> None:
        module = self._module()
        completion = module.CompletionIdentity(
            run_id="run-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            sender_terminal_id="terminal-worker",
        )
        assignment = module.ActiveAssignment(
            role=module.AssignmentRole.WORKER,
            worker_node="worker-node-1",
            task_id="task-1",
            attempt=1,
            dispatch_id="dispatch-1",
            terminal_id="terminal-worker",
            launch_mode=module.LaunchMode.BARE_BACKGROUND,
            completion_identity=completion,
        )
        projections = tuple(
            module.EventProjection(
                kind=module.EventProjectionKind.QUESTION,
                message_id=message_id,
                completion_identity=completion,
                outcome=None,
                body_digest="sha256:" + digit * 64,
            )
            for message_id, digit in (("message-1", "1"), ("message-2", "2"))
        )
        delivery = module.PendingDelivery(
            delivery_id="delivery-1",
            consumer_generation=1,
            ordered_message_ids=("message-1", "message-2"),
            ordered_event_projection=projections,
            delivery_digest=module.delivery_content_digest(
                delivery_id="delivery-1",
                consumer_generation=1,
                ordered_message_ids=("message-1", "message-2"),
                ordered_event_projection=projections,
            ),
            ack_operation_id=None,
            ack_status=module.AckStatus.PENDING,
        )
        draft = module.WorkflowCheckpointDraft(
            root=self._root(),
            run=module.RunIdentity("run-1", "terminal-main", 1),
            workflow_sequence=4,
            task_sequence=1,
            execution_mode=module.ExecutionMode.SERIAL,
            workflow_state=module.CheckpointState.QUESTION,
            task_policy=module.TaskPolicyReference(
                4,
                "team-1",
                "/tmp/agent-team-workspace",
                "task-1",
                1,
                "sha256:" + "4" * 64,
            ),
            active_assignment=assignment,
            pending_delivery=delivery,
            replied_message_ids=("message-2", "message-1"),
            read_observed=False,
            released=False,
            review_authority=None,
            verification_authority=None,
            last_operation=None,
        )
        checkpoint = module._issue_checkpoint(draft, updated_ns=20, issuer=object())
        self.assertEqual(
            ("message-2", "message-1"),
            module.decode_checkpoint(
                module.encode_checkpoint(checkpoint)
            ).replied_message_ids,
        )

    def test_operation_intent_task_sequence_is_action_specific(self) -> None:
        module = self._module()
        root = self._root()
        start = module.OperationIntent(
            operation_id="operation-start",
            effect_key="effect-start",
            root_key=root.root_key,
            root=root,
            action=module.OperationAction.START,
            request_digest="sha256:" + "1" * 64,
            expected_workflow_sequence=0,
            expected_task_sequence=None,
            run_id=None,
            main_terminal_id=None,
            task_id=None,
            dispatch_id=None,
            attempt=None,
            terminal_id=None,
            delivery_id=None,
            message_id=None,
            consumer_generation=0,
            owner="owner-1",
            lease_epoch=0,
            fencing_token=0,
            actor="actor-1",
            evidence_ref=None,
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(start, next_task_sequence=1)

        prompt = module.OperationIntent(
            operation_id="operation-prompt",
            effect_key="effect-prompt",
            root_key=root.root_key,
            root=None,
            action=module.OperationAction.PROMPT,
            request_digest="sha256:" + "2" * 64,
            expected_workflow_sequence=2,
            expected_task_sequence=None,
            run_id="run-1",
            main_terminal_id="terminal-main",
            task_id=None,
            dispatch_id=None,
            attempt=None,
            terminal_id=None,
            delivery_id=None,
            message_id=None,
            consumer_generation=0,
            owner="owner-1",
            lease_epoch=0,
            fencing_token=0,
            actor="actor-1",
            evidence_ref=None,
            next_task_sequence=1,
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(prompt, next_task_sequence=None)
        existing_prompt = dataclasses.replace(
            prompt,
            expected_task_sequence=1,
            next_task_sequence=None,
        )
        self.assertEqual(1, existing_prompt.expected_task_sequence)
        with self.assertRaises(ValueError):
            dataclasses.replace(existing_prompt, next_task_sequence=2)
        with self.assertRaises(ValueError):
            dataclasses.replace(
                prompt,
                action=module.OperationAction.WAIT,
                task_id="task-1",
                dispatch_id="dispatch-1",
                attempt=1,
                terminal_id="terminal-worker",
            )

    def test_transition_rejects_bool_task_sequence(self) -> None:
        module = self._module()
        with self.assertRaises((TypeError, ValueError)):
            module.PolicyOrVerificationTransition(
                kind=module.TransitionKind.POLICY,
                root_key="root-1",
                authority=module.AuthorityReference(
                    "policy-ref",
                    "sha256:" + "1" * 64,
                ),
                expected_workflow_sequence=2,
                expected_task_sequence=None,
                next_task_sequence=True,
                actor="policy-authority",
                request_digest="sha256:" + "2" * 64,
            )

    def test_operation_and_receipt_bind_root_run_main_terminal_and_digests(
        self,
    ) -> None:
        module = self._module()
        root = self._root()
        start = module.OperationIntent(
            operation_id="operation-start",
            effect_key="effect-start",
            root_key="root-1",
            root=root,
            action=module.OperationAction.START,
            request_digest="sha256:" + "1" * 64,
            expected_workflow_sequence=0,
            expected_task_sequence=None,
            run_id=None,
            main_terminal_id=None,
            task_id=None,
            dispatch_id=None,
            attempt=None,
            terminal_id=None,
            delivery_id=None,
            message_id=None,
            consumer_generation=0,
            owner="owner-1",
            lease_epoch=0,
            fencing_token=0,
            actor="actor-1",
            evidence_ref="sha256:" + "2" * 64,
        )
        self.assertEqual(
            module.operation_intent_digest(start, intent_sequence=1),
            module.operation_intent_digest(start, intent_sequence=1),
        )
        issuer = object()
        receipt = module._issue_durable_receipt(
            issuer=issuer,
            receipt_id="receipt-1",
            operation_id=start.operation_id,
            effect_key=start.effect_key,
            action=start.action,
            request_digest=start.request_digest,
            root_key=start.root_key,
            run_id="run-1",
            main_terminal_id="terminal-main",
            task_id=None,
            dispatch_id=None,
            attempt=None,
            terminal_id=None,
            delivery_id=None,
            message_id=None,
            consumer_generation=0,
            owner=start.owner,
            lease_epoch=start.lease_epoch,
            fencing_token=start.fencing_token,
            effect_ref="effect-ref-1",
            result_kind="started",
            result_digest="sha256:" + "3" * 64,
            evidence_ref="sha256:" + "4" * 64,
            issued_ns=21,
        )
        self.assertEqual("terminal-main", receipt.main_terminal_id)
        self.assertEqual(
            module.durable_receipt_digest(receipt),
            module.durable_receipt_digest(receipt),
        )
        with self.assertRaises(ValueError):
            dataclasses.replace(start, root_key="different-root")

    def test_physical_store_errors_are_not_duplicated_by_pure_codec(self) -> None:
        module = self._module()
        for name in (
            "StoreSchemaError",
            "StoreMigrationRequiredError",
            "StoreIntegrityError",
            "StoreCommitUnknownError",
        ):
            self.assertFalse(hasattr(module, name), name)

    def test_opaque_identifiers_are_ascii_and_reject_secret_like_values(self) -> None:
        module = self._module()
        with self.assertRaises(ValueError):
            dataclasses.replace(self._root(), root_key="root-秘密")
        with self.assertRaises(ValueError):
            dataclasses.replace(self._root(), team_id="api_key")
        with self.assertRaises(ValueError):
            module.AuthorityReference(
                reference="access_token",
                digest="sha256:" + "1" * 64,
            )

    def test_task_policy_reference_binds_root_and_checkpoint_sequence(self) -> None:
        draft = self._draft()
        assert draft.task_policy is not None
        with self.assertRaises(ValueError):
            dataclasses.replace(
                draft,
                task_policy=dataclasses.replace(
                    draft.task_policy,
                    sequence=draft.task_sequence + 1,
                ),
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                draft,
                task_policy=dataclasses.replace(
                    draft.task_policy,
                    team_id="team-foreign",
                ),
            )
