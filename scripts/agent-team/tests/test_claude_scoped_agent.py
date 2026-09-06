from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "agent_team" / "claude_scoped_agent.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for the scoped ACP wrapper")
class ClaudeScopedAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-scoped-")
        self.root = Path(self.directory.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
        (self.workspace / "src" / "blocked").mkdir()
        (self.workspace / "src" / "blocked" / "secret.py").write_text(
            "secret\n", encoding="utf-8"
        )
        self.state_root = (self.root / "state").resolve()
        self.state_root.mkdir()
        self.private_root = (self.root / "private").resolve()
        self.private_root.mkdir()
        self.runtime_root = (self.root / "runtime").resolve()
        self.runtime_root.mkdir()
        self.policy = {
            "workspace": str(self.workspace),
            "allowed_paths": ["src/"],
            "forbidden_paths": ["src/blocked/"],
            "permission": "workspace-write",
            "protected_paths": [
                str(self.state_root),
                str(self.private_root),
                str(self.runtime_root),
            ],
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_entrypoint_keeps_library_logs_off_protocol_stdout_and_disposes(
        self,
    ) -> None:
        package = (self.root / "adapter").resolve()
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "version": "0.70.0",
                    "type": "module",
                }
            )
        )
        entry = package / "dist" / "index.js"
        entry.write_text("")
        (package / "dist" / "lib.js").write_text("""export class SettingsManager {
  constructor() {}
  async initialize() {}
  getSettings() { return {permissions: {defaultMode: "default"}}; }
  dispose() {}
}
export function runAcp() {
  console.log("library diagnostic");
  process.stdout.write('{"jsonrpc":"2.0","result":{}}\\n');
  const agent = Object.fromEntries(["newSession", "loadSession", "resumeSession", "unstable_forkSession", "setSessionMode", "setSessionConfigOption", "authenticate", "logout", "unstable_listProviders", "unstable_setProvider", "unstable_disableProvider", "listSessions", "deleteSession", "steer", "goal"].map(name => [name, async () => ({})]));
  agent.dispose = async () => { process.stderr.write("disposed\\n"); };
  return {agent, connection: {closed: Promise.resolve()}};
}""")
        policy = {
            **self.policy,
            "protected_paths": [*self.policy["protected_paths"], str(package)],
        }
        policy_file = self.private_root / "policy.json"
        policy_file.write_text(json.dumps(policy))
        policy_file.chmod(0o600)
        result = subprocess.run(
            [
                NODE,
                str(WRAPPER),
                "--agent-entry",
                str(entry),
                "--policy",
                str(policy_file),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"jsonrpc":"2.0","result":{}}\n')
        self.assertIn("library diagnostic", result.stderr)
        self.assertIn("disposed", result.stderr)

    def test_resolve_agent_library_returns_the_canonical_sibling(self) -> None:
        package = (self.root / "adapter").resolve()
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "version": "0.70.0",
                }
            )
        )
        entry = package / "dist" / "index.js"
        entry.write_text("", encoding="utf-8")
        library = package / "dist" / "lib.js"
        library.write_text("export const runAcp = () => ({})\n", encoding="utf-8")
        resolved = self.run_probe("resolveAgentLibrary", str(entry))
        self.assertEqual(resolved, library.as_uri())

    def test_resolve_agent_library_rejects_a_symlink_sibling(self) -> None:
        package = (self.root / "adapter").resolve()
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@agentclientprotocol/claude-agent-acp",
                    "version": "0.70.0",
                }
            )
        )
        entry = package / "dist" / "index.js"
        entry.write_text("", encoding="utf-8")
        outside = self.root / "outside-lib.js"
        outside.write_text("export const untrusted = true\n", encoding="utf-8")
        (package / "dist" / "lib.js").symlink_to(outside)
        message = self.run_probe_error("resolveAgentLibrary", str(entry))
        self.assertIn("symlink", message)

    def run_probe(self, operation: str, *arguments: object) -> object:
        assert NODE is not None
        script = """
 const scoped = await import(process.argv[1]);
const operation = process.argv[2];
const args = JSON.parse(process.argv[3]);
try {
  const result = await scoped[operation](...args);
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
                str(WRAPPER),
                operation,
                json.dumps(arguments, ensure_ascii=False),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        if not payload["ok"]:
            raise AssertionError(payload["error"])
        return payload["result"]

    def run_probe_error(self, operation: str, *arguments: object) -> str:
        assert NODE is not None
        script = """
 const scoped = await import(process.argv[1]);
const operation = process.argv[2];
const args = JSON.parse(process.argv[3]);
try {
  const result = await scoped[operation](...args);
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
                str(WRAPPER),
                operation,
                json.dumps(arguments, ensure_ascii=False),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        return str(payload["error"])

    def test_valid_write_and_edit_are_explicitly_allowed(self) -> None:
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                decision = self.run_probe(
                    "decideTool",
                    self.policy,
                    tool,
                    {"file_path": str(self.workspace / "src" / "ok.py")},
                )
                self.assertEqual(decision["behavior"], "allow")

    def test_forbidden_path_wins_over_allowed_path(self) -> None:
        decision = self.run_probe(
            "decideTool",
            self.policy,
            "Edit",
            {"file_path": str(self.workspace / "src" / "blocked" / "secret.py")},
        )
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("forbidden", decision["message"])

    def test_outside_escape_symlink_hardlink_and_protected_paths_are_denied(
        self,
    ) -> None:
        outside = self.root / "outside.py"
        outside.write_text("outside\n", encoding="utf-8")
        symlink = self.workspace / "src" / "link.py"
        symlink.symlink_to(outside)
        hardlink = self.workspace / "src" / "hard.py"
        os.link(outside, hardlink)
        cases = (
            ("outside", str(outside)),
            ("escape", str(self.workspace / "src" / ".." / "outside.py")),
            ("symlink", str(symlink)),
            ("hardlink", str(hardlink)),
            ("protected", str(self.state_root / "state.json")),
        )
        for label, path in cases:
            with self.subTest(label=label):
                decision = self.run_probe(
                    "decideTool", self.policy, "Write", {"file_path": path}
                )
                self.assertEqual(decision["behavior"], "deny")

    def test_unknown_bash_mcp_and_terminal_tools_are_denied(self) -> None:
        for tool in ("Bash", "mcp__evil__write", "terminal", "NotebookEdit"):
            with self.subTest(tool=tool):
                decision = self.run_probe(
                    "decideTool",
                    self.policy,
                    tool,
                    {"file_path": str(self.workspace / "src" / "ok.py")},
                )
                self.assertEqual(decision["behavior"], "deny")

    def test_pre_tool_use_hook_returns_explicit_allow_or_deny(self) -> None:
        allowed = self.run_probe(
            "hookDecision",
            self.policy,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.workspace / "src" / "ok.py")},
            },
        )
        denied = self.run_probe(
            "hookDecision",
            self.policy,
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo unsafe"},
            },
        )
        self.assertEqual(allowed["behavior"], "allow")
        self.assertEqual(denied["behavior"], "deny")

    def test_reads_are_workspace_wide_but_protected_reads_are_denied(self) -> None:
        outside_allowed = self.workspace / "docs.md"
        outside_allowed.write_text("docs\n", encoding="utf-8")
        read_decision = self.run_probe(
            "decideTool",
            self.policy,
            "Read",
            {"file_path": str(outside_allowed)},
        )
        protected_decision = self.run_probe(
            "decideTool",
            self.policy,
            "Read",
            {"file_path": str(self.state_root / "state.json")},
        )
        grep_default = self.run_probe("decideTool", self.policy, "Grep", {})
        self.assertEqual(read_decision["behavior"], "allow")
        self.assertEqual(protected_decision["behavior"], "deny")
        self.assertEqual(grep_default["behavior"], "allow")

    def test_glob_pattern_cannot_escape_or_recurse_into_protected_subtrees(
        self,
    ) -> None:
        absolute = self.run_probe(
            "decideTool", self.policy, "Glob", {"pattern": "/etc/**"}
        )
        parent = self.run_probe("decideTool", self.policy, "Glob", {"pattern": "../**"})
        relative = self.run_probe(
            "decideTool", self.policy, "Glob", {"pattern": "src/**/*.py"}
        )
        (self.workspace / "src" / ".git").mkdir()
        protected_recursive = self.run_probe(
            "decideTool", self.policy, "Glob", {"pattern": "src/**/*.py"}
        )
        self.assertEqual(absolute["behavior"], "deny")
        self.assertEqual(parent["behavior"], "deny")
        self.assertEqual(relative["behavior"], "allow")
        self.assertEqual(protected_recursive["behavior"], "deny")

    def test_normal_repo_named_config_files_follow_task_scope(self) -> None:
        for name in ("AGENTS.md", "settings.json", "state.json", ".agent"):
            target = self.workspace / "src" / name
            if name == ".agent":
                target.mkdir()
                target = target / "notes.md"
            else:
                target.write_text("allowed by task\n", encoding="utf-8")
            with self.subTest(name=name):
                decision = self.run_probe(
                    "decideTool",
                    self.policy,
                    "Write",
                    {"file_path": str(target)},
                )
                self.assertEqual(decision["behavior"], "allow")

    def test_case_ambiguous_leaf_is_denied(self) -> None:
        (self.workspace / "src" / "case.py").write_text("case\n", encoding="utf-8")
        decision = self.run_probe(
            "decideTool",
            self.policy,
            "Write",
            {"file_path": str(self.workspace / "src" / "Case.py")},
        )
        self.assertEqual(decision["behavior"], "deny")

    def test_malformed_policy_and_unknown_fields_fail_closed(self) -> None:
        missing = dict(self.policy)
        del missing["protected_paths"]
        self.run_probe_error("parsePolicy", missing)
        unknown = {**self.policy, "task_id": "unexpected"}
        self.run_probe_error("parsePolicy", unknown)
        relative_protected = {**self.policy, "protected_paths": ["state"]}
        self.run_probe_error("parsePolicy", relative_protected)
        missing_permission = dict(self.policy)
        del missing_permission["permission"]
        self.run_probe_error("parsePolicy", missing_permission)
        invalid_permission = {**self.policy, "permission": "orchestrator"}
        self.run_probe_error("parsePolicy", invalid_permission)

    def test_read_only_policy_denies_writes_and_removes_write_tools(self) -> None:
        policy = {**self.policy, "permission": "read-only"}
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                decision = self.run_probe(
                    "decideTool",
                    policy,
                    tool,
                    {"file_path": str(self.workspace / "src" / "ok.py")},
                )
                self.assertEqual(decision["behavior"], "deny")
        injected = self.run_probe(
            "injectSessionParams",
            {
                "cwd": str(self.workspace),
                "mcpServers": [],
                "additionalDirectories": [],
            },
            policy,
        )
        options = injected["_meta"]["claudeCode"]["options"]
        self.assertEqual(options["tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(options["allowedTools"], options["tools"])
        self.assertIn("Write", options["disallowedTools"])
        self.assertIn("Edit", options["disallowedTools"])

    def test_ambient_non_default_permission_modes_fail_closed(self) -> None:
        for mode in ("acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan"):
            with self.subTest(mode=mode):
                message = self.run_probe_error("assertAmbientDefaultMode", mode)
                self.assertIn("ambient permission mode", message)
        self.assertTrue(self.run_probe("assertAmbientDefaultMode", "default"))
        self.assertTrue(self.run_probe("assertAmbientDefaultMode", "manual"))

    def test_session_injection_replaces_security_options_and_disables_bypass(
        self,
    ) -> None:
        params = {
            "cwd": str(self.workspace),
            "mcpServers": [],
            "additionalDirectories": [],
            "_meta": {
                "claudeCode": {
                    "options": {
                        "model": "fable",
                        "allowedTools": ["Bash"],
                        "tools": ["Bash"],
                        "permissionMode": "bypassPermissions",
                        "settings": {
                            "permissions": {"defaultMode": "bypassPermissions"}
                        },
                    }
                }
            },
        }
        message = self.run_probe_error("injectSessionParams", params, self.policy)
        self.assertIn("override", message)

        params["_meta"]["claudeCode"]["options"] = {
            "model": "fable",
            "allowedTools": ["Read", "Grep", "Glob", "Write", "Edit"],
        }
        injected = self.run_probe("injectSessionParams", params, self.policy)
        options = injected["_meta"]["claudeCode"]["options"]
        self.assertEqual(options["tools"], ["Read", "Grep", "Glob", "Write", "Edit"])
        self.assertEqual(options["allowedTools"], options["tools"])
        self.assertEqual(options["mcpServers"], {})
        self.assertEqual(options["additionalDirectories"], [])
        self.assertEqual(options["settingSources"], [])
        self.assertFalse(options["persistSession"])
        self.assertFalse(options["settings"]["autoMemoryEnabled"])
        permissions = options["settings"]["permissions"]
        self.assertEqual(permissions["defaultMode"], "default")
        self.assertEqual(permissions["disableBypassPermissionsMode"], "disable")
        self.assertNotIn("permissionMode", options)
        self.assertNotIn("allowDangerouslySkipPermissions", options)

    def test_policy_file_requires_private_regular_0600_file(self) -> None:
        policy_path = self.root / "write-policy.json"
        policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        policy_path.chmod(0o644)
        message = self.run_probe_error("loadPolicy", str(policy_path))
        self.assertIn("0600", message)

    def test_all_session_entrypoints_receive_the_same_fixed_options(self) -> None:
        assert NODE is not None
        script = """
const scoped = await import(process.argv[1]);
const policy = JSON.parse(process.argv[2]);
const seen = {};
const methods = ["newSession", "loadSession", "resumeSession", "unstable_forkSession"];
const agent = {};
for (const method of methods) {
  agent[method] = async (params) => {
    seen[method] = params;
    return {ok: true};
  };
}
agent.setSessionMode = async (params) => ({ok: true, params});
agent.setSessionConfigOption = async (params) => ({ok: true, params});
for (const method of ["authenticate", "logout", "unstable_listProviders", "unstable_setProvider", "unstable_disableProvider", "listSessions", "deleteSession", "steer", "goal"]) {
  agent[method] = async () => ({ok: true});
}
scoped.installSessionGuards(agent, policy);
for (const method of methods) {
  await agent[method]({cwd: policy.workspace, mcpServers: [], additionalDirectories: []});
}
const summary = {};
for (const method of methods) {
  const options = seen[method]._meta.claudeCode.options;
  summary[method] = {
    tools: options.tools,
    allowedTools: options.allowedTools,
    settingSources: options.settingSources,
    additionalDirectories: options.additionalDirectories,
    mcpServers: options.mcpServers,
            defaultMode: options.settings.permissions.defaultMode,
            bypass: options.settings.permissions.disableBypassPermissionsMode,
            autoMemoryEnabled: options.settings.autoMemoryEnabled,
  };
}
let modeError = "";
let configError = "";
try { await agent.setSessionMode({sessionId: "s", modeId: "bypassPermissions"}); }
catch (error) { modeError = String(error?.message ?? error); }
try { await agent.setSessionConfigOption({sessionId: "s", configId: "mode", value: "acceptEdits"}); }
catch (error) { configError = String(error?.message ?? error); }
summary.modeError = modeError;
summary.configError = configError;
let providerError = "";
try { await agent.unstable_setProvider({}); }
catch (error) { providerError = String(error?.message ?? error); }
summary.providerError = providerError;
process.stdout.write(JSON.stringify(summary));
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(WRAPPER),
                json.dumps(self.policy),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        for method in (
            "newSession",
            "loadSession",
            "resumeSession",
            "unstable_forkSession",
        ):
            with self.subTest(method=method):
                self.assertEqual(
                    summary[method]["tools"], ["Read", "Grep", "Glob", "Write", "Edit"]
                )
                self.assertEqual(
                    summary[method]["allowedTools"], summary[method]["tools"]
                )
                self.assertEqual(summary[method]["settingSources"], [])
                self.assertEqual(summary[method]["additionalDirectories"], [])
                self.assertEqual(summary[method]["mcpServers"], {})
                self.assertEqual(summary[method]["defaultMode"], "default")
                self.assertEqual(summary[method]["bypass"], "disable")
                self.assertFalse(summary[method]["autoMemoryEnabled"])
        self.assertIn("not allowed", summary["modeError"])
        self.assertIn("not allowed", summary["configError"])
        self.assertIn("not allowed", summary["providerError"])


if __name__ == "__main__":
    unittest.main()
