from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "agent_team" / "codex_scoped_bridge.mjs"
FILE_TOOLS = ROOT / "agent_team" / "scoped_file_tools.mjs"
NODE = shutil.which("node")


def canonical(value: object) -> str:
    if isinstance(value, dict):
        value = {key: value[key] for key in sorted(value)}
    if isinstance(value, list):
        value = [canonical_value(item) for item in value]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical_value(item) for item in value]
    return value


@unittest.skipUnless(NODE, "Node.js is required for the Codex scoped bridge")
class CodexScopedBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-codex-bridge-")
        self.root = Path(self.directory.name)
        self.workspace = (self.root / "workspace").resolve()
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "read.txt").write_text("hello\n", encoding="utf-8")
        (self.protected / "secret.txt").write_text("protected\n", encoding="utf-8")
        self.policy = {
            "workspace": str(self.workspace),
            "allowed_paths": ["src/"],
            "forbidden_paths": ["src/blocked/"],
            "protected_paths": [str(self.protected)],
            "permission": "workspace-write",
        }
        self.config = {
            "config": {
                "model": "gpt-test",
                "model_reasoning_effort": "medium",
                "model_provider": "openai",
                "mcp_servers": {
                    "existing": {"command": "never-run", "enabled": True},
                },
            },
            "origins": {},
            "layers": [],
        }
        encoded = canonical_value(self.config)
        self.snapshot = hashlib.sha256(
            json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def protected(self) -> Path:
        value = getattr(self, "_protected", None)
        if value is None:
            value = (self.root / "protected").resolve()
            value.mkdir()
            self._protected = value
        return value

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_probe(self, script: str, *args: object) -> object:
        assert NODE is not None
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(BRIDGE),
                str(FILE_TOOLS),
                *[json.dumps(arg, ensure_ascii=False) for arg in args],
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def test_exports_contract_and_config_snapshot_is_canonical(self) -> None:
        result = self.run_probe(
            """
            const bridge = await import(process.argv[1]);
            const first = {config: {b: 2, a: 1}, origins: {}, layers: []};
            const second = {layers: [], origins: {}, config: {a: 1, b: 2}};
            process.stdout.write(JSON.stringify({
              bridge: typeof bridge.CodexScopeBridge,
              snapshot: bridge.configSnapshot(first) === bridge.configSnapshot(second),
            }));
            """,
        )
        self.assertEqual(result, {"bridge": "function", "snapshot": True})

    def test_initialize_and_skills_are_fixed_and_skills_stays_local(self) -> None:
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const calls = [];
            const child = async (method, params) => {
              calls.push({method, params});
              return {codexHome: "/private", platformFamily: "unix", platformOs: "macos", userAgent: "test"};
            };
            const policy = JSON.parse(process.argv[3]);
            const bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: "0000000000000000000000000000000000000000000000000000000000000000"}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller", version: "bad"}, capabilities: null});
            const skills = await bridge.handleClientRequest("skills/list", {cwds: [policy.workspace], forceReload: true});
            process.stdout.write(JSON.stringify({calls, skills}));
            """,
            self.policy,
        )
        self.assertEqual([item["method"] for item in result["calls"]], ["initialize"])
        self.assertEqual(
            result["skills"]["data"],
            [{"cwd": str(self.workspace), "skills": [], "errors": []}],
        )
        self.assertEqual(
            result["calls"][0]["params"]["capabilities"],
            {"experimentalApi": True, "requestAttestation": False},
        )

    def test_thread_start_reads_requirements_and_config_then_rebuilds_fixed_child_params(
        self,
    ) -> None:
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const {DYNAMIC_FILE_TOOLS} = await import(process.argv[2]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const calls = [];
            const child = async (method, params) => {
              calls.push({method, params});
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return config;
              if (method === "thread/start") return {
                thread: {id: "thread-1", cwd: policy.workspace, model: "gpt-test", ephemeral: true, reasoningEffort: "medium"},
                cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium",
                approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null},
              };
              throw new Error(`unexpected ${method}`);
            };
            const bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[5])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            const response = await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, modelProvider: null, config: {projects: {[policy.workspace]: {trust_level: "trusted"}}}});
            const thread = calls.find((item) => item.method === "thread/start");
            process.stdout.write(JSON.stringify({response, calls, thread: thread.params, dynamic: DYNAMIC_FILE_TOOLS}));
            """,
            self.policy,
            self.config,
            self.snapshot,
        )
        self.assertEqual(
            [item["method"] for item in result["calls"]],
            ["initialize", "configRequirements/read", "config/read", "thread/start"],
        )
        child = result["thread"]
        self.assertEqual(child["cwd"], str(self.workspace))
        self.assertEqual(child["model"], "gpt-test")
        self.assertEqual(child["modelProvider"], "openai")
        self.assertEqual(child["permissions"], ":read-only")
        self.assertEqual(child["approvalPolicy"], "never")
        self.assertEqual(child["environments"], [])
        self.assertTrue(child["ephemeral"])
        self.assertFalse(child["allowProviderModelFallback"])
        self.assertEqual(
            child["config"]["mcp_servers"], {"existing": {"enabled": False}}
        )
        self.assertEqual(child["config"]["model_reasoning_effort"], "medium")
        self.assertEqual(
            [item["name"] for item in child["dynamicTools"]],
            ["read_text", "write_text", "edit_text", "list_files"],
        )
        self.assertIn("role", child["developerInstructions"])
        self.assertIn("ファイル操作ツール", child["developerInstructions"])
        self.assertEqual(result["response"]["thread"]["id"], "thread-1")

    def test_turn_start_and_notification_before_response_are_bound_to_one_turn(
        self,
    ) -> None:
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const calls = [];
            let bridge;
            const child = async (method, params) => {
              calls.push({method, params});
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return config;
              if (method === "thread/start") return {thread: {id: "thread-1", cwd: policy.workspace, model: "gpt-test", ephemeral: true, reasoningEffort: "medium"}, cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium", approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null}};
              if (method === "turn/start") {
                await bridge.handleNotification("turn/started", {threadId: "thread-1", turn: {id: "turn-1", status: "inProgress"}});
                return {turn: {id: "turn-1", status: "inProgress"}};
              }
              return {};
            };
            bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[5])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, modelProvider: null, config: {projects: {[policy.workspace]: {trust_level: "trusted"}}}});
            const response = await bridge.handleClientRequest("turn/start", {threadId: "thread-1", input: [{type: "text", text: "hello", text_elements: []}], model: "gpt-test", effort: "medium", approvalPolicy: "on-request", approvalsReviewer: "user", sandboxPolicy: {type: "workspaceWrite", writableRoots: [], networkAccess: false, excludeTmpdirEnvVar: false, excludeSlashTmp: false}});
            await bridge.handleNotification("turn/completed", {threadId: "thread-1", turn: {id: "turn-1", status: "completed"}});
            process.stdout.write(JSON.stringify({response, calls}));
            """,
            self.policy,
            self.config,
            self.snapshot,
        )
        turn = next(item for item in result["calls"] if item["method"] == "turn/start")
        self.assertEqual(turn["params"]["threadId"], "thread-1")
        self.assertEqual(turn["params"]["model"], "gpt-test")
        self.assertEqual(turn["params"]["effort"], "medium")
        self.assertEqual(turn["params"]["permissions"], ":read-only")
        self.assertEqual(turn["params"]["approvalPolicy"], "never")
        self.assertEqual(turn["params"]["environments"], [])
        self.assertEqual(
            result["response"], {"turn": {"id": "turn-1", "status": "inProgress"}}
        )

    def test_dynamic_tool_call_executes_allowed_file_tool_and_rejects_replay_and_cross_id(
        self,
    ) -> None:
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const calls = [];
            const child = async (method, params) => {
              calls.push({method, params});
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return config;
              if (method === "thread/start") return {thread: {id: "thread-1", cwd: policy.workspace, model: "gpt-test", ephemeral: true, reasoningEffort: "medium"}, cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium", approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null}};
              if (method === "turn/start") return {turn: {id: "turn-1", status: "inProgress"}};
              return {};
            };
            const bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[5])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, modelProvider: null, config: {projects: {[policy.workspace]: {trust_level: "trusted"}}}});
            await bridge.handleClientRequest("turn/start", {threadId: "thread-1", input: [{type: "text", text: "hello", text_elements: []}]});
            const ok = await bridge.handleServerRequest("item/tool/call", {threadId: "thread-1", turnId: "turn-1", callId: "call-1", tool: "read_text", namespace: null, arguments: {file_path: policy.workspace + "/src/read.txt", offset: 1, limit: 1}});
            const replay = await bridge.handleServerRequest("item/tool/call", {threadId: "thread-1", turnId: "turn-1", callId: "call-1", tool: "read_text", namespace: null, arguments: {file_path: policy.workspace + "/src/read.txt", offset: 1, limit: 1}}).then(() => "accepted", (error) => String(error.message));
            const cross = await bridge.handleServerRequest("item/tool/call", {threadId: "other", turnId: "turn-1", callId: "call-2", tool: "read_text", namespace: null, arguments: {file_path: policy.workspace + "/src/read.txt", offset: 1, limit: 1}}).then(() => "accepted", (error) => String(error.message));
            process.stdout.write(JSON.stringify({ok, replay, cross}));
            """,
            self.policy,
            self.config,
            self.snapshot,
        )
        self.assertEqual(result["ok"]["success"], True)
        self.assertEqual(result["ok"]["contentItems"][0]["type"], "inputText")
        self.assertEqual(
            json.loads(result["ok"]["contentItems"][0]["text"]),
            {
                "file_path": str(self.workspace / "src" / "read.txt"),
                "content": "hello",
                "start_line": 1,
                "end_line": 1,
                "total_lines": 1,
                "truncated": False,
            },
        )
        self.assertIn("already used", result["replay"])
        self.assertIn("ids", result["cross"])

    def test_read_only_tool_write_and_unknown_client_methods_do_not_mutate(
        self,
    ) -> None:
        read_policy = {**self.policy, "permission": "read-only"}
        target = self.workspace / "src" / "read-only.txt"
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const child = async (method) => {
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return config;
              if (method === "thread/start") return {thread: {id: "thread-1", cwd: policy.workspace, model: "gpt-test", ephemeral: true, reasoningEffort: "medium"}, cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium", approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null}};
              if (method === "turn/start") return {turn: {id: "turn-1", status: "inProgress"}};
              return {};
            };
            const bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[5])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, modelProvider: null, config: {projects: {[policy.workspace]: {trust_level: "trusted"}}}});
            await bridge.handleClientRequest("turn/start", {threadId: "thread-1", input: [{type: "text", text: "hello", text_elements: []}]});
            const write = await bridge.handleServerRequest("item/tool/call", {threadId: "thread-1", turnId: "turn-1", callId: "call-write", tool: "write_text", namespace: null, arguments: {file_path: JSON.parse(process.argv[6]), content: "changed"}});
            const unknown = await bridge.handleClientRequest("thread/resume", {} ).then(() => "accepted", (error) => String(error.message));
            process.stdout.write(JSON.stringify({write, unknown}));
            """,
            read_policy,
            self.config,
            self.snapshot,
            str(target),
        )
        self.assertFalse(result["write"]["success"])
        self.assertIn(
            "dynamic tool is not allowed", result["write"]["contentItems"][0]["text"]
        )
        self.assertFalse(target.exists())
        self.assertIn("unknown client method", result["unknown"])

    def test_late_line_read_uses_host_contract_and_title_thread_stays_local(
        self,
    ) -> None:
        (self.workspace / "src" / "read.txt").write_text("row\n" * 3_000)
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const calls = [];
            const child = async (method, params) => {
              calls.push({method, params});
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return config;
              if (method === "thread/start") return {thread: {id: "thread-1", cwd: policy.workspace, model: "gpt-test", ephemeral: true, reasoningEffort: "medium"}, cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium", approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null}};
              if (method === "turn/start") return {turn: {id: "turn-1", status: "inProgress"}};
              return {};
            };
            const bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[5])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, modelProvider: null, config: {projects: {[policy.workspace]: {trust_level: "trusted"}}}});
            await bridge.handleClientRequest("turn/start", {threadId: "thread-1", input: [{type: "text", text: "hello", text_elements: []}]});
            const read = await bridge.handleServerRequest("item/tool/call", {threadId: "thread-1", turnId: "turn-1", callId: "read-late", tool: "read_text", arguments: {file_path: `${policy.workspace}/src/read.txt`, offset: 2501, limit: 1}});
            const beforeTitle = calls.length;
            const title = await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, ephemeral: true}).then(() => "accepted", (error) => error.message);
            bridge.handleNotification("turn/completed", {threadId: "thread-1", turn: {id: "turn-1", status: "completed"}});
            const closed = await bridge.handleServerRequest("item/tool/call", {threadId: "thread-1", turnId: "turn-1", callId: "read-closed", tool: "read_text", arguments: {file_path: `${policy.workspace}/src/read.txt`, offset: 1, limit: 1}}).then(() => "accepted", (error) => error.message);
            process.stdout.write(JSON.stringify({read, title, closed, beforeTitle, afterTitle: calls.length}));
            """,
            self.policy,
            self.config,
            self.snapshot,
        )
        self.assertTrue(result["read"]["success"], result["read"])
        self.assertEqual(
            json.loads(result["read"]["contentItems"][0]["text"])["start_line"], 2501
        )
        self.assertEqual(result["beforeTitle"], result["afterTitle"])
        self.assertIn("only one thread/start", result["title"])
        self.assertIn("turn ended", result["closed"])

    def test_pending_turn_can_be_interrupted_after_its_started_notification(
        self,
    ) -> None:
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const calls = [];
            let bridge, resolveTurn;
            const child = async (method, params) => {
              calls.push({method, params});
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return config;
              if (method === "thread/start") return {thread: {id: "thread-1", cwd: policy.workspace, ephemeral: true}, cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium", approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null}};
              if (method === "turn/start") return new Promise((resolve) => {resolveTurn = resolve;});
              if (method === "turn/interrupt") {
                bridge.handleNotification("turn/completed", {threadId: "thread-1", turn: {id: "turn-1", status: "interrupted"}});
                return {};
              }
              return {};
            };
            bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[5])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            await bridge.handleClientRequest("thread/start", {cwd: policy.workspace, modelProvider: null, config: {projects: {[policy.workspace]: {trust_level: "trusted"}}}});
            const started = bridge.handleClientRequest("turn/start", {threadId: "thread-1", input: [{type: "text", text: "hello", text_elements: []}]});
            const unknown = await bridge.handleClientRequest("turn/interrupt", {threadId: "thread-1", turnId: "turn-1"}).then(() => "accepted", (error) => error.message);
            bridge.handleNotification("turn/started", {threadId: "thread-1", turn: {id: "turn-1", status: "inProgress"}});
            const interrupted = await bridge.handleClientRequest("turn/interrupt", {threadId: "thread-1", turnId: "turn-1"}).then(() => "accepted", (error) => error.message);
            resolveTurn({turn: {id: "turn-1", status: "inProgress"}});
            await started;
            const tool = await bridge.handleServerRequest("item/tool/call", {threadId: "thread-1", turnId: "turn-1", callId: "after-interrupt", tool: "read_text", arguments: {file_path: `${policy.workspace}/src/read.txt`, offset: 1, limit: 1}}).then(() => "accepted", (error) => error.message);
            process.stdout.write(JSON.stringify({unknown, interrupted, tool, interrupts: calls.filter((call) => call.method === "turn/interrupt")}));
            """,
            self.policy,
            self.config,
            self.snapshot,
        )
        self.assertNotEqual(result["unknown"], "accepted")
        self.assertEqual(result["interrupted"], "accepted")
        self.assertIn("turn ended", result["tool"])
        self.assertEqual(len(result["interrupts"]), 1)
        self.assertEqual(
            result["interrupts"][0]["params"],
            {"threadId": "thread-1", "turnId": "turn-1"},
        )

    def test_config_drift_is_rejected(self) -> None:
        changed = {**self.config, "config": {**self.config["config"], "model": "other"}}
        result = self.run_probe(
            """
            const {CodexScopeBridge} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const config = JSON.parse(process.argv[4]);
            const changed = JSON.parse(process.argv[5]);
            const calls = [];
            const child = async (method, params) => {
              calls.push({method, params});
              if (method === "initialize") return {};
              if (method === "configRequirements/read") return {requirements: null};
              if (method === "config/read") return changed;
              if (method === "thread/start") return {thread: {id: "thread-1", cwd: policy.workspace, model: "gpt-test", ephemeral: true, reasoningEffort: "medium"}, cwd: policy.workspace, model: "gpt-test", modelProvider: "openai", reasoningEffort: "medium", approvalPolicy: "never", approvalsReviewer: "user", activePermissionProfile: {id: ":read-only", extends: null}};
              return {};
            };
            const bridge = new CodexScopeBridge({policy, model: "gpt-test", effort: "medium", instructions: "role", configSnapshot: JSON.parse(process.argv[6])}, child);
            await bridge.handleClientRequest("initialize", {clientInfo: {name: "caller"}});
            const drift = await bridge.handleClientRequest("config/read", {}).then(() => "accepted", (error) => String(error.message));
            process.stdout.write(JSON.stringify({drift, calls}));
            """,
            self.policy,
            self.config,
            changed,
            self.snapshot,
        )
        self.assertIn("snapshot", result["drift"])
        self.assertEqual(
            [item["method"] for item in result["calls"]], ["initialize", "config/read"]
        )


if __name__ == "__main__":
    unittest.main()
