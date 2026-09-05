from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team import workflow_store as workflow
from agent_team.backup import BackupIntegrityError, SQLiteBackup
from agent_team.lease import StoreImageObservation
from agent_team.recovery import RestoreLedger
from agent_team.restore import BackupRestore, RestoreReviewRequiredError
from agent_team.store import CoordinationStore


def _state_root(parent: str) -> Path:
    root = Path(os.path.realpath(parent)) / "state"
    root.mkdir()
    root.chmod(0o700)
    return root


def _root_identity(parent: str, state_root: Path) -> workflow.RootIdentity:
    workspace = Path(os.path.realpath(parent)) / "workspace"
    workspace.mkdir()
    config = workspace / "config.toml"
    config_bytes = b"team = 'team-1'\n"
    config.write_bytes(config_bytes)
    workspace_stat = workspace.stat()
    config_stat = config.stat()
    state_stat = state_root.stat()
    return workflow.RootIdentity(
        root_key="root-1",
        team_id="team-1",
        workspace=workflow.PathIdentity(
            str(workspace), workspace_stat.st_dev, workspace_stat.st_ino
        ),
        config_path=str(config),
        config_device=config_stat.st_dev,
        config_inode=config_stat.st_ino,
        config_digest=workflow.config_content_digest(config_bytes),
        state_root=workflow.PathIdentity(
            str(state_root), state_stat.st_dev, state_stat.st_ino
        ),
    )


def _insert_start_intent(store: CoordinationStore, root: workflow.RootIdentity) -> None:
    store.begin_operation(
        workflow.OperationIntent(
            operation_id="operation-start",
            effect_key="effect/start",
            root_key=root.root_key,
            root=root,
            action=workflow.OperationAction.START,
            request_digest=workflow.digest_bounded_body(
                b"start", domain=workflow.REQUEST_DIGEST_DOMAIN
            ),
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
        ),
        expected_workflow_sequence=0,
        expected_task_sequence=None,
    )


class WorkflowStoreBackupRestoreTest(unittest.TestCase):
    def test_backup_final_readback_rejects_workflow_count_drift(self) -> None:
        class DriftingBackup(SQLiteBackup):
            observation_calls = 0

            def _store_image(self, fd: int) -> StoreImageObservation:
                observation = super()._store_image(fd)
                type(self).observation_calls += 1
                if type(self).observation_calls == 6:
                    return replace(
                        observation,
                        workflow_row_counts=(1, 0, 0, 0),
                    )
                return observation

        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-backup-drift-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root):
                pass
            with self.assertRaises(BackupIntegrityError) as raised:
                DriftingBackup(state_root).create("workflow-drift")
            self.assertIn("final backup inspect", str(raised.exception))

    def test_exact_legacy_v2_pair_is_not_a_v3_inspect_or_restore_input(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-legacy-backup-"
        ) as temporary:
            state_root = _state_root(temporary)
            with CoordinationStore(state_root):
                pass
            backup = SQLiteBackup(state_root)
            current_artifact = backup.create("legacy-snapshot")
            database = state_root / current_artifact.database_basename
            connection = sqlite3.connect(str(database), isolation_level=None)
            try:
                connection.execute("PRAGMA foreign_keys = OFF")
                for trigger in (
                    "workflow_receipts_require_committed",
                    "workflow_receipts_no_update",
                    "workflow_receipts_no_delete",
                    "workflow_receipts_no_replace",
                    "workflow_events_receipt_matches_operation",
                    "workflow_events_no_update",
                    "workflow_events_no_delete",
                    "workflow_events_no_replace",
                ):
                    connection.execute(f"DROP TRIGGER {trigger}")
                connection.execute("DROP INDEX workflow_operations_root_status_idx")
                connection.execute("DROP INDEX workflow_events_operation_idx")
                for table in (
                    "workflow_events",
                    "workflow_receipts",
                    "workflow_operations",
                    "workflow_checkpoints",
                ):
                    connection.execute(f"DROP TABLE {table}")
                connection.execute(
                    "UPDATE store_meta SET value = 2 WHERE key = 'store_schema'"
                )
                connection.execute("PRAGMA user_version = 2")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
            database_bytes = database.read_bytes()
            manifest = current_artifact.manifest
            legacy_manifest = {
                "version": 1,
                "database_basename": current_artifact.database_basename,
                "store_schema": 2,
                "event_schema_version": 2,
                "sqlite_user_version": 2,
                "integrity_check": "ok",
                "database_size": len(database_bytes),
                "database_digest": "sha256:"
                + hashlib.sha256(database_bytes).hexdigest(),
                "captured_recovery_epoch": manifest.captured_recovery_epoch,
                "captured_fencing_token_floor": (manifest.captured_fencing_token_floor),
            }
            manifest_path = state_root / current_artifact.manifest_basename
            manifest_path.write_bytes(
                json.dumps(
                    legacy_manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            manifest_path.chmod(0o600)
            with self.assertRaises(BackupIntegrityError):
                backup.inspect(current_artifact.database_basename)
            with (
                mock.patch.object(
                    BackupRestore,
                    "_hold_quiescence",
                    side_effect=AssertionError(
                        "legacy rejection must precede quiescence"
                    ),
                ),
                self.assertRaises(BackupIntegrityError),
            ):
                BackupRestore(state_root).restore(
                    current_artifact,
                    actor="operator",
                    audit_ref="audit/legacy-v2",
                )

    def test_backup_inspect_preserves_counts_and_source_restore_rejects_pre_quiescence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-backup-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root_identity(temporary, state_root)
            with CoordinationStore(state_root) as store:
                _insert_start_intent(store, root)
            backup = SQLiteBackup(state_root)
            artifact = backup.create("workflow-snapshot")
            self.assertEqual((1, 1, 0, 1), artifact.workflow_row_counts)
            self.assertEqual(artifact, backup.inspect("workflow-snapshot"))
            with (
                mock.patch.object(
                    BackupRestore,
                    "_hold_quiescence",
                    side_effect=AssertionError(
                        "source rejection must precede quiescence"
                    ),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(state_root).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/workflow-source",
                )

    def test_current_primary_workflow_rows_reject_before_restore_ledger(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="agent-team-workflow-restore-"
        ) as temporary:
            state_root = _state_root(temporary)
            root = _root_identity(temporary, state_root)
            with CoordinationStore(state_root):
                pass
            backup = SQLiteBackup(state_root)
            artifact = backup.create("empty-snapshot")
            self.assertFalse(artifact.workflow_rows_present)
            with CoordinationStore(state_root) as store:
                _insert_start_intent(store, root)
            with (
                mock.patch.object(
                    RestoreLedger,
                    "read_for_resume",
                    side_effect=AssertionError(
                        "primary rejection must precede restore ledger mutation"
                    ),
                ),
                self.assertRaises(RestoreReviewRequiredError),
            ):
                BackupRestore(state_root).restore(
                    artifact,
                    actor="operator",
                    audit_ref="audit/workflow-primary",
                )


if __name__ == "__main__":
    unittest.main()
