from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "agent_team" / "scoped_file_tools.mjs"
NODE = shutil.which("node")
MAX_FILE_BYTES = 10_000_000
MAX_READ_BYTES = 1_048_576


@unittest.skipUnless(NODE, "Node.js is required for scoped file tools")
class ScopedFileToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-file-tools-")
        self.root = Path(self.directory.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "nested").mkdir()
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "hidden.txt").write_text(
            "must stay hidden\n", encoding="utf-8"
        )
        self.protected = (self.root / "protected").resolve()
        self.protected.mkdir()
        (self.protected / "secret.txt").write_text("protected\n", encoding="utf-8")
        self.outside = (self.root / "outside").resolve()
        self.outside.mkdir()
        (self.outside / "sentinel.txt").write_text(
            "outside sentinel\n", encoding="utf-8"
        )
        self.policy = {
            "workspace": str(self.workspace),
            "allowed_paths": ["src/"],
            "forbidden_paths": ["src/blocked/"],
            "protected_paths": [str(self.protected)],
            "permission": "workspace-write",
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_tool(
        self, tool: str, arguments: object, policy: dict | None = None
    ) -> object:
        assert NODE is not None
        script = """
const scoped = await import(process.argv[1]);
import fs from "node:fs";
const [policy, tool, arguments_] = JSON.parse(process.argv[2]);
if (arguments_?.content === "__TEST_OVERSIZED__") {
  arguments_.content = "x".repeat(10000001);
}
const failWrite = arguments_?.__TEST_FAIL_WRITE === true;
if (failWrite) delete arguments_.__TEST_FAIL_WRITE;
const failCleanup = arguments_?.__TEST_FAIL_CLEANUP === true;
if (failCleanup) delete arguments_.__TEST_FAIL_CLEANUP;
const requireWriteBeforeFchmod = arguments_?.__TEST_REQUIRE_WRITE_BEFORE_FCHMOD === true;
if (requireWriteBeforeFchmod) delete arguments_.__TEST_REQUIRE_WRITE_BEFORE_FCHMOD;
let writeCalls = 0;
if (requireWriteBeforeFchmod) {
  const originalWrite = fs.writeSync;
  const originalFchmod = fs.fchmodSync;
  fs.writeSync = (...values) => {
    writeCalls += 1;
    return originalWrite(...values);
  };
  fs.fchmodSync = (...values) => {
    if (writeCalls === 0) throw new Error("fchmod happened before write");
    return originalFchmod(...values);
  };
}
if (failWrite) {
  const originalWrite = fs.writeSync;
  fs.writeSync = (...values) => {
    const count = originalWrite(...values);
    throw new Error("injected write failure");
  };
}
if (failCleanup) {
  fs.unlinkSync = () => { throw new Error("injected cleanup failure"); };
}
try {
  const result = await scoped.executeFileTool(policy, tool, arguments_);
  process.stdout.write(JSON.stringify({ok: true, result}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error?.message ?? error)}));
}
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(MODULE),
                json.dumps(
                    [policy if policy is not None else self.policy, tool, arguments],
                    ensure_ascii=False,
                ),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        if not payload["ok"]:
            raise AssertionError(payload["error"])
        return payload["result"]

    def run_tool_error(
        self, tool: str, arguments: object, policy: dict | None = None
    ) -> str:
        assert NODE is not None
        script = """
const scoped = await import(process.argv[1]);
import fs from "node:fs";
const [policy, tool, arguments_] = JSON.parse(process.argv[2]);
if (arguments_?.content === "__TEST_OVERSIZED__") {
  arguments_.content = "x".repeat(10000001);
}
const failWrite = arguments_?.__TEST_FAIL_WRITE === true;
if (failWrite) delete arguments_.__TEST_FAIL_WRITE;
const failCleanup = arguments_?.__TEST_FAIL_CLEANUP === true;
if (failCleanup) delete arguments_.__TEST_FAIL_CLEANUP;
const requireWriteBeforeFchmod = arguments_?.__TEST_REQUIRE_WRITE_BEFORE_FCHMOD === true;
if (requireWriteBeforeFchmod) delete arguments_.__TEST_REQUIRE_WRITE_BEFORE_FCHMOD;
let writeCalls = 0;
if (requireWriteBeforeFchmod) {
  const originalWrite = fs.writeSync;
  const originalFchmod = fs.fchmodSync;
  fs.writeSync = (...values) => {
    writeCalls += 1;
    return originalWrite(...values);
  };
  fs.fchmodSync = (...values) => {
    if (writeCalls === 0) throw new Error("fchmod happened before write");
    return originalFchmod(...values);
  };
}
if (failWrite) {
  const originalWrite = fs.writeSync;
  fs.writeSync = (...values) => {
    const count = originalWrite(...values);
    throw new Error("injected write failure");
  };
}
if (failCleanup) {
  fs.unlinkSync = () => { throw new Error("injected cleanup failure"); };
}
try {
  const result = await scoped.executeFileTool(policy, tool, arguments_);
  process.stdout.write(JSON.stringify({ok: true, result}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error?.message ?? error)}));
}
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(MODULE),
                json.dumps(
                    [policy if policy is not None else self.policy, tool, arguments],
                    ensure_ascii=False,
                ),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        return str(payload["error"])

    def test_dynamic_tools_are_fixed_strict_function_schemas(self) -> None:
        tools = self.run_probe("DYNAMIC_FILE_TOOLS")
        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "read_text",
                "write_text",
                "edit_text",
                "list_files",
            ],
        )
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["type"], "function")
                self.assertIsInstance(tool["description"], str)
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
        edit_schema = next(tool for tool in tools if tool["name"] == "edit_text")[
            "inputSchema"
        ]
        self.assertEqual(edit_schema["properties"]["old_string"]["minLength"], 1)

    def run_probe(self, export_name: str) -> object:
        assert NODE is not None
        script = """
const scoped = await import(process.argv[1]);
process.stdout.write(JSON.stringify(scoped[process.argv[2]]));
"""
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(MODULE), export_name],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_read_and_write_positive_paths_preserve_existing_mode(self) -> None:
        new_file = self.workspace / "src" / "new.txt"
        written = self.run_tool(
            "write_text", {"file_path": str(new_file), "content": "one\ntwo\n"}
        )
        self.assertEqual(written["file_path"], str(new_file))
        self.assertEqual(new_file.read_text(encoding="utf-8"), "one\ntwo\n")
        self.assertEqual(stat.S_IMODE(new_file.stat().st_mode), 0o644)

        existing = self.workspace / "src" / "existing.txt"
        existing.write_text("old\n", encoding="utf-8")
        existing.chmod(0o600)
        self.run_tool("write_text", {"file_path": str(existing), "content": "new\n"})
        self.assertEqual(existing.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o600)

        executable = self.workspace / "src" / "executable.txt"
        executable.write_text("old\n", encoding="utf-8")
        executable.chmod(0o755)
        self.run_tool("write_text", {"file_path": str(executable), "content": "new\n"})
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)

    def test_mutation_rejects_special_mode_bits_before_any_effect(self) -> None:
        target = self.workspace / "src" / "setuid.txt"
        target.write_text("sentinel\n", encoding="utf-8")
        target.chmod(0o4755)
        before = target.read_bytes()
        before_mode = stat.S_IMODE(target.stat().st_mode)
        self.assertTrue(before_mode & 0o7000)
        error = self.run_tool_error(
            "write_text", {"file_path": str(target), "content": "changed\n"}
        )
        self.assertIn("special mode", error)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), before_mode)

    def test_write_failure_keeps_existing_and_new_targets_unchanged(self) -> None:
        existing = self.workspace / "src" / "failure-existing.txt"
        existing.write_text("original\n", encoding="utf-8")
        before = existing.read_bytes()
        error = self.run_tool_error(
            "write_text",
            {
                "file_path": str(existing),
                "content": "replacement\n",
                "__TEST_FAIL_WRITE": True,
            },
        )
        self.assertIn("injected write failure", error)
        self.assertEqual(existing.read_bytes(), before)

        edit_target = self.workspace / "src" / "failure-edit.txt"
        edit_target.write_text("before\n", encoding="utf-8")
        edit_before = edit_target.read_bytes()
        self.assertIn(
            "injected write failure",
            self.run_tool_error(
                "edit_text",
                {
                    "file_path": str(edit_target),
                    "old_string": "before",
                    "new_string": "after",
                    "replace_all": False,
                    "__TEST_FAIL_WRITE": True,
                },
            ),
        )
        self.assertEqual(edit_target.read_bytes(), edit_before)

        new_target = self.workspace / "src" / "failure-new.txt"
        self.assertTrue(
            self.run_tool_error(
                "write_text",
                {
                    "file_path": str(new_target),
                    "content": "replacement\n",
                    "__TEST_FAIL_WRITE": True,
                },
            )
        )
        self.assertFalse(new_target.exists())
        self.assertFalse(
            any(
                name.startswith(".agent-team-")
                for name in os.listdir(new_target.parent)
            )
        )

    def test_existing_mode_is_applied_only_after_temp_write(self) -> None:
        target = self.workspace / "src" / "mode-order.txt"
        target.write_text("before\n", encoding="utf-8")
        target.chmod(0o755)
        self.run_tool(
            "write_text",
            {
                "file_path": str(target),
                "content": "after\n",
                "__TEST_REQUIRE_WRITE_BEFORE_FCHMOD": True,
            },
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_failed_cleanup_keeps_primary_error_and_reports_temp_path(self) -> None:
        target = self.workspace / "src" / "cleanup-failure.txt"
        target.write_text("before\n", encoding="utf-8")
        error = self.run_tool_error(
            "write_text",
            {
                "file_path": str(target),
                "content": "after\n",
                "__TEST_FAIL_WRITE": True,
                "__TEST_FAIL_CLEANUP": True,
            },
        )
        self.assertIn("injected write failure", error)
        self.assertIn("temporary cleanup unconfirmed", error)
        self.assertIn(".agent-team-", error)
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(
            len(
                [
                    name
                    for name in os.listdir(target.parent)
                    if name.startswith(".agent-team-")
                ]
            ),
            1,
        )

    def test_bom_is_valid_and_preserved_for_read_and_edit(self) -> None:
        target = self.workspace / "src" / "bom.txt"
        target.write_bytes(b"\xef\xbb\xbfA\n")
        read = self.run_tool(
            "read_text", {"file_path": str(target), "offset": 1, "limit": 1}
        )
        self.assertEqual(read["content"], "\ufeffA")
        self.run_tool(
            "edit_text",
            {
                "file_path": str(target),
                "old_string": "A",
                "new_string": "B",
                "replace_all": False,
            },
        )
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfB\n")
        self.run_tool(
            "write_text",
            {"file_path": str(target), "content": "\ufeffC\n"},
        )
        self.assertEqual(target.read_bytes(), b"\xef\xbb\xbfC\n")

    def test_read_returns_requested_line_range_and_total(self) -> None:
        target = self.workspace / "src" / "lines.txt"
        target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        result = self.run_tool(
            "read_text",
            {"file_path": str(target), "offset": 2, "limit": 2},
        )
        self.assertEqual(result["start_line"], 2)
        self.assertEqual(result["end_line"], 3)
        self.assertEqual(result["total_lines"], 4)
        self.assertEqual(result["content"], "two\nthree")
        self.assertFalse(result["truncated"])

    def test_read_output_is_bounded_and_explicitly_truncated(self) -> None:
        target = self.workspace / "src" / "large-line.txt"
        target.write_bytes(b"x" * (MAX_READ_BYTES + 1024) + b"\n")
        result = self.run_tool(
            "read_text",
            {"file_path": str(target), "offset": 1, "limit": 1},
        )
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"].encode("utf-8")), MAX_READ_BYTES)

    def test_edit_requires_found_unambiguous_old_text_and_leaves_failure_unchanged(
        self,
    ) -> None:
        absent = self.workspace / "src" / "absent-edit.txt"
        self.assertTrue(
            self.run_tool_error(
                "edit_text",
                {
                    "file_path": str(absent),
                    "old_string": "missing",
                    "new_string": "x",
                    "replace_all": False,
                },
            )
        )
        self.assertFalse(absent.exists())

        target = self.workspace / "src" / "edit.txt"
        target.write_text("a b a\n", encoding="utf-8")
        before = target.read_bytes()
        error = self.run_tool_error(
            "edit_text",
            {
                "file_path": str(target),
                "old_string": "missing",
                "new_string": "x",
                "replace_all": False,
            },
        )
        self.assertIn("not found", error)
        self.assertEqual(target.read_bytes(), before)
        error = self.run_tool_error(
            "edit_text",
            {
                "file_path": str(target),
                "old_string": "a",
                "new_string": "x",
                "replace_all": False,
            },
        )
        self.assertIn("ambiguous", error)
        self.assertEqual(target.read_bytes(), before)
        result = self.run_tool(
            "edit_text",
            {
                "file_path": str(target),
                "old_string": "a",
                "new_string": "x",
                "replace_all": True,
            },
        )
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "x b x\n")

    def test_read_only_policy_denies_both_mutations_without_changes(self) -> None:
        target = self.workspace / "src" / "readonly.txt"
        target.write_text("unchanged\n", encoding="utf-8")
        before = target.read_bytes()
        readonly = {**self.policy, "permission": "read-only"}
        for tool, arguments in (
            (
                "write_text",
                {"file_path": str(target), "content": "changed\n"},
            ),
            (
                "edit_text",
                {
                    "file_path": str(target),
                    "old_string": "unchanged",
                    "new_string": "changed",
                    "replace_all": False,
                },
            ),
        ):
            with self.subTest(tool=tool):
                error = self.run_tool_error(tool, arguments, readonly)
                self.assertIn("not allowed", error)
                self.assertEqual(target.read_bytes(), before)

    def test_policy_denies_forbidden_outside_git_and_protected_paths(self) -> None:
        forbidden = self.workspace / "src" / "blocked"
        forbidden.mkdir()
        forbidden_file = forbidden / "blocked.txt"
        forbidden_file.write_text("forbidden\n", encoding="utf-8")
        protected_file = self.protected / "secret.txt"
        cases = (
            ("forbidden", forbidden_file),
            ("outside", self.outside / "sentinel.txt"),
            ("git", self.workspace / ".git" / "hidden.txt"),
            ("protected", protected_file),
        )
        for label, target in cases:
            with self.subTest(label=label):
                error = self.run_tool_error(
                    "write_text", {"file_path": str(target), "content": "nope\n"}
                )
                self.assertTrue(error)
        self.assertEqual(forbidden_file.read_text(encoding="utf-8"), "forbidden\n")
        self.assertEqual(
            (self.outside / "sentinel.txt").read_text(), "outside sentinel\n"
        )

    def test_directory_symlink_hardlink_and_special_targets_are_rejected(self) -> None:
        directory = self.workspace / "src" / "directory"
        directory.mkdir()
        self.assertTrue(
            self.run_tool_error(
                "write_text", {"file_path": str(directory), "content": "nope"}
            )
        )

        source = self.workspace / "src" / "source.txt"
        source.write_text("sentinel\n", encoding="utf-8")
        symlink = self.workspace / "src" / "symlink.txt"
        symlink.symlink_to(source)
        hardlink = self.workspace / "src" / "hardlink.txt"
        os.link(source, hardlink)
        fifo = self.workspace / "src" / "fifo"
        os.mkfifo(fifo)
        for target in (symlink, hardlink, fifo):
            with self.subTest(target=target.name):
                self.assertTrue(
                    self.run_tool_error(
                        "write_text", {"file_path": str(target), "content": "nope"}
                    )
                )
        self.assertEqual(source.read_text(encoding="utf-8"), "sentinel\n")

    def test_parent_symlink_and_exact_directory_promotion_cannot_escape(self) -> None:
        parent_link = self.workspace / "src" / "linked-parent"
        parent_link.symlink_to(self.outside, target_is_directory=True)
        outside_target = self.outside / "created.txt"
        self.assertTrue(
            self.run_tool_error(
                "write_text",
                {"file_path": str(parent_link / "created.txt"), "content": "nope"},
            )
        )
        self.assertFalse(outside_target.exists())

        promoted = self.workspace / "src" / "promoted"
        promoted.mkdir()
        self.assertTrue(
            self.run_tool_error(
                "write_text", {"file_path": str(promoted), "content": "nope"}
            )
        )
        self.assertTrue(promoted.is_dir())

    def test_invalid_unknown_arguments_and_unknown_tool_fail_closed(self) -> None:
        target = self.workspace / "src" / "args.txt"
        cases = (
            ("read_text", {"file_path": str(target), "offset": 0, "limit": 1}),
            ("read_text", {"file_path": str(target), "offset": 1, "limit": 2001}),
            ("read_text", {"file_path": str(target), "offset": 1}),
            (
                "write_text",
                {"file_path": str(target), "content": "x", "extra": True},
            ),
            (
                "edit_text",
                {
                    "file_path": str(target),
                    "old_string": "x",
                    "new_string": "y",
                },
            ),
            (
                "edit_text",
                {
                    "file_path": str(target),
                    "old_string": "",
                    "new_string": "y",
                    "replace_all": False,
                },
            ),
            (
                "list_files",
                {"directory": str(self.workspace), "recursive": "yes", "limit": 1},
            ),
            ("not_a_tool", {}),
        )
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=arguments):
                self.assertTrue(self.run_tool_error(tool, arguments))
        self.assertFalse(target.exists())
        versioned_policy = {**self.policy, "version": 1}
        self.assertTrue(
            self.run_tool_error(
                "write_text",
                {"file_path": str(target), "content": "x"},
                versioned_policy,
            )
        )

    def test_non_utf8_existing_files_are_rejected_before_mutation(self) -> None:
        target = self.workspace / "src" / "invalid.txt"
        target.write_bytes(b"valid prefix\xff\n")
        before = target.read_bytes()
        for tool, arguments in (
            ("write_text", {"file_path": str(target), "content": "replacement"}),
            (
                "edit_text",
                {
                    "file_path": str(target),
                    "old_string": "valid",
                    "new_string": "changed",
                    "replace_all": False,
                },
            ),
        ):
            with self.subTest(tool=tool):
                self.assertIn("UTF-8", self.run_tool_error(tool, arguments))
                self.assertEqual(target.read_bytes(), before)

    def test_file_size_limit_is_enforced_before_new_and_existing_writes(self) -> None:
        new_target = self.workspace / "src" / "oversized-new.txt"
        self.assertIn(
            "10 MB",
            self.run_tool_error(
                "write_text",
                {"file_path": str(new_target), "content": "__TEST_OVERSIZED__"},
            ),
        )
        self.assertFalse(new_target.exists())

        existing = self.workspace / "src" / "oversized-existing.txt"
        existing.write_text("small\n", encoding="utf-8")
        before = existing.read_bytes()
        self.assertIn(
            "10 MB",
            self.run_tool_error(
                "write_text",
                {"file_path": str(existing), "content": "__TEST_OVERSIZED__"},
            ),
        )
        self.assertEqual(existing.read_bytes(), before)

    def test_list_files_is_sorted_bounded_and_skips_non_regular_entries(self) -> None:
        (self.workspace / "src" / "b.txt").write_text("b\n", encoding="utf-8")
        (self.workspace / "src" / "a.txt").write_text("a\n", encoding="utf-8")
        (self.workspace / "src" / "nested" / "c.txt").write_text(
            "c\n", encoding="utf-8"
        )
        (self.workspace / "src" / "nested" / ".git").mkdir()
        (self.workspace / "src" / "nested" / ".git" / "hidden").write_text(
            "hidden\n", encoding="utf-8"
        )
        source = self.workspace / "src" / "source-list.txt"
        source.write_text("source\n", encoding="utf-8")
        (self.workspace / "src" / "link-list.txt").symlink_to(source)
        os.link(source, self.workspace / "src" / "hard-list.txt")
        os.mkfifo(self.workspace / "src" / "fifo-list")

        result = self.run_tool(
            "list_files",
            {"directory": str(self.workspace / "src"), "recursive": True, "limit": 2},
        )
        files = result["files"]
        self.assertEqual(files, sorted(files))
        self.assertEqual(len(files), 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(any(".git" in value for value in files))
        self.assertFalse(any("link-list" in value for value in files))
        self.assertFalse(any("hard-list" in value for value in files))
        self.assertFalse(any("fifo-list" in value for value in files))

    def test_list_files_stops_after_visited_entry_bound(self) -> None:
        for index in range(5_001):
            (self.workspace / "src" / f"entry-{index:04d}").symlink_to(
                self.workspace / "src" / "nested"
            )
        result = self.run_tool(
            "list_files",
            {"directory": str(self.workspace / "src"), "recursive": False, "limit": 1},
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(result["visited_entries"], 5_000)
        self.assertEqual(result["files"], [])

    def test_list_result_is_bounded_by_one_megabyte(self) -> None:
        long_parent = self.workspace / "src" / ("p" * 220) / ("q" * 220) / ("r" * 220)
        long_parent.mkdir(parents=True)
        for index in range(1_001):
            (long_parent / f"entry-{index:04d}-{'x' * 180}").write_text(
                "x\n", encoding="utf-8"
            )
        result = self.run_tool(
            "list_files",
            {"directory": str(long_parent), "recursive": False, "limit": 1_000},
        )
        self.assertTrue(result["truncated"])
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 1_048_576
        )


if __name__ == "__main__":
    unittest.main()
