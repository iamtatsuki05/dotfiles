from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team.workspace_revision import (
    WorkspaceRevisionError,
    changed_paths,
    snapshot_manifest,
    snapshot_revision,
)


class WorkspaceRevisionTest(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", "-C", str(root), *args),
            check=True,
            capture_output=True,
            text=True,
        )

    def _repo(self, temp_dir: str) -> Path:
        root = Path(temp_dir) / "repo"
        root.mkdir()
        self._git(root, "init", "-q")
        self._git(root, "config", "user.name", "Workspace Revision Test")
        self._git(root, "config", "user.email", "revision@example.test")
        return root

    def _commit_all(self, root: Path, message: str = "initial") -> None:
        self._git(root, "add", "--all")
        self._git(root, "commit", "-qm", message)

    def test_non_git_workspace_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "plain"
            workspace.mkdir()
            (workspace / "file.txt").write_text("content", encoding="utf-8")

            with self.assertRaisesRegex(WorkspaceRevisionError, "Git"):
                snapshot_revision(workspace)

    def test_same_content_has_same_revision_and_ignored_content_is_excluded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._repo(temp_dir)
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "tracked.txt").write_text("same\n", encoding="utf-8")
            self._commit_all(root)

            first = snapshot_revision(root)
            second = snapshot_revision(root)
            self.assertEqual(first, second)

            (root / "ignored.txt").write_text("ignored-v1\n", encoding="utf-8")
            self.assertEqual(first, snapshot_revision(root))
            (root / "untracked.txt").write_text("untracked-v1\n", encoding="utf-8")
            self.assertNotEqual(first, snapshot_revision(root))

    def test_worktree_index_head_and_executable_changes_have_distinct_revisions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._repo(temp_dir)
            tracked = root / "tracked.txt"
            executable = root / "run.sh"
            tracked.write_text("one\n", encoding="utf-8")
            executable.write_text("#!/bin/sh\nprintf ok\n", encoding="utf-8")
            self._commit_all(root)
            baseline = snapshot_revision(root)

            tracked.write_text("two\n", encoding="utf-8")
            modified = snapshot_revision(root)
            self.assertNotEqual(baseline, modified)
            tracked.write_text("one\n", encoding="utf-8")
            self.assertEqual(baseline, snapshot_revision(root))

            executable.chmod(executable.stat().st_mode | 0o111)
            executable_revision = snapshot_revision(root)
            self.assertNotEqual(baseline, executable_revision)
            executable.chmod((executable.stat().st_mode & ~0o111) | 0o001)
            self.assertNotEqual(executable_revision, snapshot_revision(root))
            executable.chmod(executable.stat().st_mode & ~0o111)
            self.assertEqual(baseline, snapshot_revision(root))

            tracked.unlink()
            deleted_manifest = snapshot_manifest(root)
            self.assertEqual(
                deleted_manifest.entry("tracked.txt").kind,
                "deleted",
            )
            deleted_revision = deleted_manifest.revision
            self.assertNotEqual(baseline, deleted_revision)
            tracked.write_text("one\n", encoding="utf-8")

            tracked.write_text("staged\n", encoding="utf-8")
            self._git(root, "add", "tracked.txt")
            tracked.write_text("one\n", encoding="utf-8")
            staged_revision = snapshot_revision(root)
            self.assertNotEqual(baseline, staged_revision)
            self._git(root, "reset", "-q", "HEAD", "--", "tracked.txt")
            self.assertEqual(baseline, snapshot_revision(root))

            self._git(root, "rm", "-q", "tracked.txt")
            staged_deleted = snapshot_manifest(root)
            self.assertEqual(staged_deleted.entry("tracked.txt").kind, "deleted")
            self._git(root, "restore", "--staged", "--worktree", "--", "tracked.txt")
            self.assertEqual(baseline, snapshot_revision(root))

            self._git(root, "commit", "--allow-empty", "-qm", "head changed")
            self.assertNotEqual(baseline, snapshot_revision(root))

    def test_changed_paths_reports_added_modified_and_deleted_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._repo(temp_dir)
            (root / "same.txt").write_text("same\n", encoding="utf-8")
            (root / "changed.txt").write_text("before\n", encoding="utf-8")
            (root / "deleted.txt").write_text("gone\n", encoding="utf-8")
            self._commit_all(root)
            before = snapshot_manifest(root)

            (root / "changed.txt").write_text("after\n", encoding="utf-8")
            (root / "deleted.txt").unlink()
            (root / "added.txt").write_text("new\n", encoding="utf-8")
            after = snapshot_manifest(root)

            self.assertEqual(
                changed_paths(before, after),
                ("added.txt", "changed.txt", "deleted.txt"),
            )

    def test_external_symlink_is_refused_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._repo(temp_dir)
            outside = Path(temp_dir) / "outside.txt"
            outside.write_text("outside secret\n", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(outside)

            with self.assertRaisesRegex(WorkspaceRevisionError, "symlink"):
                snapshot_revision(root)

    def test_mutation_during_read_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._repo(temp_dir)
            target = root / "large.txt"
            target.write_bytes(b"a" * 100_000)
            self._commit_all(root)
            real_read = os.read
            mutated = False

            def read_and_mutate(fd: int, count: int) -> bytes:
                nonlocal mutated
                data = real_read(fd, count)
                if data and not mutated:
                    mutated = True
                    target.write_bytes(b"b" * 100_000)
                return data

            with (
                mock.patch(
                    "agent_team.workspace_revision._read_chunk", read_and_mutate
                ),
                self.assertRaisesRegex(WorkspaceRevisionError, "changed"),
            ):
                snapshot_revision(root)
            self.assertTrue(mutated)


if __name__ == "__main__":
    unittest.main()
