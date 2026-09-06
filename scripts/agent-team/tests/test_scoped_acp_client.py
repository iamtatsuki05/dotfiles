from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "agent_team" / "scoped_acp_client.mjs"
WRAPPER = ROOT / "agent_team" / "claude_scoped_agent.mjs"
NODE = shutil.which("node")
SDK_ENTRY = (
    Path(os.environ["AGENT_TEAM_SDK_ENTRY"])
    if "AGENT_TEAM_SDK_ENTRY" in os.environ
    else None
)
if SDK_ENTRY is not None and not SDK_ENTRY.is_file():
    raise RuntimeError("AGENT_TEAM_SDK_ENTRY must identify the pinned ACP SDK entry")


FIXTURE = r"""
import fs from "node:fs";

const [logPath, mode] = process.argv.slice(2);
const pending = new Map();
let model = "default";
let effort = "default";

function log(event, value = undefined) {
  const line = JSON.stringify(value === undefined ? { event } : { event, value });
  fs.appendFileSync(logPath, `${line}\n`, "utf8");
}

function send(id, result) {
  process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`);
}

function sendError(id, message) {
  process.stdout.write(`${JSON.stringify({
    jsonrpc: "2.0",
    id,
    error: { code: -32000, message },
  })}\n`);
}

function configOptions() {
  return [
    {
      id: "model",
      type: "select",
      name: "Model",
      currentValue: model,
      options: [{ value: "sonnet", name: "Sonnet" }],
    },
    {
      id: "effort",
      type: "select",
      name: "Effort",
      currentValue: effort,
      options: [{ value: "high", name: "High" }],
    },
    {
      id: "ignored-config",
      type: "select",
      name: "Ignored",
      currentValue: "default",
      options: [{ value: "default", name: "Default" }],
    },
  ];
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  while (true) {
    const newline = buffer.indexOf("\n");
    if (newline < 0) return;
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (!line.trim()) continue;
    const message = JSON.parse(line);
    const method = message.method;
    log(method, message.params);
    if (method === "initialize") {
      send(message.id, {
        protocolVersion: 1,
        agentCapabilities: { sessionCapabilities: { close: {} } },
        agentInfo: { name: "fixture", version: "1" },
      });
    } else if (method === "session/new") {
      send(message.id, { sessionId: "fixture-session", configOptions: configOptions() });
    } else if (method === "session/set_config_option") {
      if (message.params.configId === "model") model = message.params.value;
      if (message.params.configId === "effort") effort = message.params.value;
      send(message.id, { configOptions: configOptions() });
    } else if (method === "session/prompt") {
      pending.set(message.params.sessionId, message.id);
      if (mode === "success") {
        process.stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          method: "session/update",
          params: {
            sessionId: message.params.sessionId,
            update: {
              sessionUpdate: "agent_message_chunk",
              messageId: "prelude-message",
              content: { type: "text", text: "I will inspect the task first. " },
            },
          },
        })}\n`);
        process.stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          method: "session/update",
          params: {
            sessionId: message.params.sessionId,
            update: {
              sessionUpdate: "agent_message_chunk",
              messageId: "final-message",
              content: { type: "text", text: "fixture output" },
            },
          },
        })}\n`);
        pending.delete(message.params.sessionId);
        send(message.id, { stopReason: "end_turn" });
      } else if (mode === "max-tokens") {
        pending.delete(message.params.sessionId);
        send(message.id, { stopReason: "max_tokens" });
      }
    } else if (method === "session/cancel") {
      const promptId = pending.get(message.params.sessionId);
      if (promptId !== undefined) {
        pending.delete(message.params.sessionId);
        send(promptId, { stopReason: "cancelled" });
      }
    } else if (method === "session/close") {
      send(message.id, {});
    } else if (method === "$/cancel_request") {
      // The SDK may send this when its cancellation signal fires; the ACP
      // session/cancel notification above is the lifecycle operation we assert.
    } else {
      sendError(message.id, `unexpected method: ${method}`);
    }
  }
});
process.stdin.on("end", () => {
  log("stdin-end");
  process.exit(0);
});
"""


@unittest.skipUnless(
    NODE and SDK_ENTRY is not None,
    "Node and AGENT_TEAM_SDK_ENTRY for ACP SDK 1.3.0 are required",
)
class ScopedAcpClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-scoped-client-")
        self.root = Path(self.directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.fixture = self.root / "fixture-agent.mjs"
        self.fixture.write_text(textwrap.dedent(FIXTURE), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _run(
        self,
        mode: str = "success",
        *,
        permission: str = "workspace-write",
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        log_path = self.root / f"{mode}.jsonl"
        agent_argv = [NODE, str(self.fixture), str(log_path), mode]
        args = [
            NODE,
            str(CLIENT),
            "--sdk-entry",
            str(SDK_ENTRY),
            "--cwd",
            str(self.workspace),
            "--permission",
            permission,
            "--model",
            "sonnet",
            "--effort",
            "high",
            "--instructions",
            "fixture instructions",
            "--timeout-ms",
            "5000",
        ]
        args += ["--agent-argv", json.dumps(agent_argv)]
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"),
        }
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=environment,
            check=False,
            input="fixture prompt",
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result, log_path

    def _events(self, log_path: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_session_request_is_accepted_by_scoped_wrapper_without_provider(
        self,
    ) -> None:
        protected = self.root / "protected"
        protected.mkdir()
        policy = {
            "permission": "workspace-write",
            "workspace": str(self.workspace.resolve()),
            "allowed_paths": ["src/"],
            "forbidden_paths": [],
            "protected_paths": [str(protected.resolve())],
        }
        script = """
const client = await import(process.argv[1]);
const wrapper = await import(process.argv[2]);
const request = client.buildSessionRequest({
  cwd: process.argv[3],
  permission: "workspace-write",
  model: "sonnet",
  instructions: "--starts-with-dashes",
});
const injected = wrapper.injectSessionParams(request, JSON.parse(process.argv[4]));
process.stdout.write(JSON.stringify({request, injected}));
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(CLIENT),
                str(WRAPPER),
                str(self.workspace.resolve()),
                json.dumps(policy),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        request_options = payload["request"]["_meta"]["claudeCode"]["options"]
        self.assertEqual(set(request_options), {"model", "tools", "allowedTools"})
        self.assertEqual(payload["request"]["mcpServers"], [])
        self.assertEqual(
            payload["request"]["_meta"]["systemPrompt"]["append"],
            "--starts-with-dashes",
        )
        fixed_options = payload["injected"]["_meta"]["claudeCode"]["options"]
        self.assertFalse(fixed_options["persistSession"])
        self.assertEqual(fixed_options["settingSources"], [])
        self.assertEqual(payload["injected"]["mcpServers"], [])

    def test_parse_cli_accepts_instruction_starting_with_dashes(self) -> None:
        script = """
const client = await import(process.argv[1]);
const parsed = client.parseCliArgs([
  "--sdk-entry", process.argv[2],
  "--agent-argv", "[\\"node\\",\\"agent\\"]",
  "--cwd", process.argv[3],
  "--permission", "read-only",
  "--model", "sonnet",
  "--effort", "high",
  "--timeout-ms", "5",
  "--instructions", "--starts-with-dashes",
]);
process.stdout.write(JSON.stringify(parsed));
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(CLIENT),
                str(SDK_ENTRY),
                str(self.workspace),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["instructions"], "--starts-with-dashes"
        )

    def test_one_connection_orders_config_prompt_close_and_frames_one_result(
        self,
    ) -> None:
        result, log_path = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            {
                "output": "fixture output",
                "session_id": "fixture-session",
                "model": "sonnet",
                "effort": "high",
                "cleanup_confirmed": True,
            },
        )
        methods = [entry["event"] for entry in self._events(log_path)]
        self.assertEqual(
            methods,
            [
                "initialize",
                "session/new",
                "session/set_config_option",
                "session/set_config_option",
                "session/prompt",
                "session/close",
                "stdin-end",
            ],
        )
        calls = self._events(log_path)
        self.assertEqual(calls[2]["value"]["configId"], "model")
        self.assertEqual(calls[2]["value"]["value"], "sonnet")
        self.assertEqual(calls[3]["value"]["configId"], "effort")
        self.assertEqual(calls[3]["value"]["value"], "high")
        self.assertEqual(calls[1]["value"]["mcpServers"], [])
        options = calls[1]["value"]["_meta"]["claudeCode"]["options"]
        self.assertEqual(options["tools"], ["Read", "Grep", "Glob", "Write", "Edit"])
        self.assertEqual(
            options["allowedTools"], ["Read", "Grep", "Glob", "Write", "Edit"]
        )
        self.assertEqual(set(options), {"model", "tools", "allowedTools"})
        self.assertEqual(
            calls[0]["value"]["clientCapabilities"],
            {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        )

    def test_read_only_permission_omits_write_tools(self) -> None:
        result, log_path = self._run(permission="read-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._events(log_path)
        options = calls[1]["value"]["_meta"]["claudeCode"]["options"]
        self.assertEqual(options["tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(options["allowedTools"], ["Read", "Grep", "Glob"])

    def test_client_does_not_create_acpx_or_claude_state(self) -> None:
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".acpx").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / "config").exists())

    def test_non_success_stop_reason_fails_without_forged_output(self) -> None:
        result, _ = self._run("max-tokens")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("max_tokens", result.stderr)

    def test_sigterm_cancels_then_closes_child(self) -> None:
        log_path = self.root / "cancel.jsonl"
        args = [
            NODE,
            str(CLIENT),
            "--sdk-entry",
            str(SDK_ENTRY),
            "--agent-argv",
            json.dumps([NODE, str(self.fixture), str(log_path), "wait"]),
            "--cwd",
            str(self.workspace),
            "--permission",
            "workspace-write",
            "--model",
            "sonnet",
            "--effort",
            "high",
            "--instructions",
            "fixture instructions",
            "--timeout-ms",
            "10000",
        ]
        environment = {**os.environ, "HOME": str(self.home)}
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            process.stdin.write("fixture prompt")
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log_path.exists() and "session/prompt" in log_path.read_text(
                    encoding="utf-8"
                ):
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            else:
                self.fail("fixture did not receive session/prompt before cancellation")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(stdout, "")
        self.assertIn("cancel", log_path.read_text(encoding="utf-8"))
        events = [entry["event"] for entry in self._events(log_path)]
        self.assertLess(events.index("session/cancel"), events.index("session/close"))
        self.assertIn("session/close", events)
        self.assertNotEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
