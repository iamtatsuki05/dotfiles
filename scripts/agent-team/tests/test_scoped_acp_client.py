from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import textwrap
import threading
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
CODEX_SDK_ENTRY = (
    Path(os.environ["AGENT_TEAM_CODEX_SDK_ENTRY"])
    if "AGENT_TEAM_CODEX_SDK_ENTRY" in os.environ
    else None
)
if CODEX_SDK_ENTRY is not None and not CODEX_SDK_ENTRY.is_file():
    raise RuntimeError("AGENT_TEAM_CODEX_SDK_ENTRY must identify ACP SDK 1.4.0")


FIXTURE = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const [logPath, mode, harness, sdkEntry] = process.argv.slice(2);
const effortId = harness === "codex" ? "reasoning_effort" : "effort";
const pending = new Map();
let model = "default";
let effort = "default";
const sdkSchema = sdkEntry
  ? await import(new URL("./schema/zod.gen.js", pathToFileURL(sdkEntry)))
  : undefined;

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

function finishQuestionPrompt(sessionId, promptId) {
  process.stdout.write(`${JSON.stringify({
    jsonrpc: "2.0",
    method: "session/update",
    params: {
      sessionId,
      update: {
        sessionUpdate: "agent_message_chunk",
        messageId: "question-final",
        content: { type: "text", text: "question fixture output" },
      },
    },
  })}\n`);
  pending.delete(sessionId);
  send(promptId, { stopReason: "end_turn" });
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
      id: effortId,
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
    if (method === "initialize" && sdkSchema) {
      const parsed = sdkSchema.zInitializeRequest.parse(message.params);
      log(method, { ...message.params, __parsedClientCapabilities: parsed.clientCapabilities });
    } else {
      log(method, message.params);
    }
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
      if (message.params.configId === effortId) effort = message.params.value;
      if (mode === "model-mismatch") model = "other-model";
      if (mode === "effort-mismatch" && message.params.configId === "effort") effort = "low";
      if (mode === "effort-reroutes-model" && message.params.configId === "effort") model = "other-model";
      send(message.id, { configOptions: configOptions() });
    } else if (method === "session/prompt") {
      pending.set(message.params.sessionId, message.id);
      if (["success", "model-mismatch", "effort-mismatch", "effort-reroutes-model", "session-close-fails"].includes(mode)) {
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
      } else if (mode === "question-no-form") {
        process.stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          method: "session/update",
          params: {
            sessionId: message.params.sessionId,
            update: {
              sessionUpdate: "tool_call",
              toolCallId: "ask-tool-no-form",
              status: "pending",
              title: "Ask a question",
              kind: "other",
              rawInput: { questions: [{ question: "Which plan?", options: [{ label: "Safe" }] }] },
              _meta: { claudeCode: { toolName: "AskUserQuestion" } },
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
              messageId: "question-no-form-final",
              content: { type: "text", text: "question fixture output" },
            },
          },
        })}\n`);
        pending.delete(message.params.sessionId);
        log("agent-final-success");
        send(message.id, { stopReason: "end_turn" });
      } else if (mode === "question" || mode === "question-ignore-cancel" || mode === "question-malformed" || mode === "question-pending-final") {
        process.stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          method: "session/update",
          params: {
            sessionId: message.params.sessionId,
            update: {
              sessionUpdate: "tool_call",
              toolCallId: "ask-tool-1",
              status: "pending",
              title: "Ask a question",
              kind: "other",
              rawInput: {
                questions: [{
                  question: "Which plan should I use?",
                  header: "Plan",
                  options: [{ label: "Safe", description: "Use the safe plan." }],
                  multiSelect: false,
                }],
              },
              _meta: { claudeCode: { toolName: "AskUserQuestion" } },
            },
          },
        })}\n`);
        process.stdout.write(`${JSON.stringify({
          jsonrpc: "2.0",
          id: 900,
          method: "elicitation/create",
          params: {
            mode: "form",
            sessionId: message.params.sessionId,
            toolCallId: "ask-tool-1",
            message: "Which plan should I use?",
            requestedSchema: mode === "question-malformed"
              ? { type: "object", properties: {}, unexpected: true }
              : {
                  type: "object",
                  properties: {
                    question_0: {
                      type: "string",
                      title: "Plan",
                      oneOf: [{ const: "safe", title: "Safe", description: "Use the safe plan." }],
                    },
                    question_0_custom: {
                      type: "string",
                      title: "Other",
                      _meta: {
                        _askUserQuestionCustomAnswer: {
                          questionId: "question_0",
                          isCustomAnswer: true,
                        },
                      },
                    },
                  },
                },
          },
        })}\n`);
        if (mode === "question-pending-final") {
          setTimeout(() => {
            process.stdout.write(`${JSON.stringify({
              jsonrpc: "2.0",
              method: "session/update",
              params: {
                sessionId: message.params.sessionId,
                update: {
                  sessionUpdate: "agent_message_chunk",
                  messageId: "question-pending-final",
                  content: { type: "text", text: "question fixture output" },
                },
              },
            })}\n`);
            pending.delete(message.params.sessionId);
            log("agent-final-success");
            send(message.id, { stopReason: "end_turn" });
          }, 100);
        }
      }
    } else if ((mode === "question" || mode === "question-ignore-cancel" || mode === "question-malformed" || mode === "question-pending-final") && message.id === 900) {
      const sessionId = [...pending.keys()][0];
      if (mode !== "question") log("agent-final-success");
      finishQuestionPrompt(sessionId, pending.get(sessionId));
    } else if (method === "session/cancel") {
      const promptId = pending.get(message.params.sessionId);
      if (promptId !== undefined) {
        pending.delete(message.params.sessionId);
        send(promptId, { stopReason: "cancelled" });
      }
      } else if (method === "session/close") {
      if (mode === "session-close-fails") {
        sendError(message.id, "fixture close failure");
      } else {
        send(message.id, {});
      }
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
        harness: str = "claude",
        question_socket: Path | None = None,
        result_file: Path | None = None,
        launch_nonce: str | None = None,
        client_prefix: tuple[str, ...] = (),
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        log_path = self.root / f"{mode}.jsonl"
        sdk_entry = CODEX_SDK_ENTRY if harness == "codex" else SDK_ENTRY
        agent_argv = [
            NODE,
            str(self.fixture),
            str(log_path),
            mode,
            harness,
            str(sdk_entry),
        ]
        args = [
            NODE,
            *client_prefix,
            str(CLIENT),
            "--harness",
            harness,
            "--sdk-entry",
            str(sdk_entry),
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
        if question_socket is not None:
            args += ["--question-socket", str(question_socket)]
        if result_file is not None:
            args += ["--result-file", str(result_file)]
        if launch_nonce is not None:
            args += ["--launch-nonce", launch_nonce]
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
  harness: "claude",
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
  "--harness", "claude",
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

    def test_selected_sdk_schema_preserves_form_capability(self) -> None:
        script = """
import { pathToFileURL } from "node:url";
const schema = await import(new URL("./schema/zod.gen.js", pathToFileURL(process.argv[1])));
const parsed = schema.zInitializeRequest.parse({
  protocolVersion: 1,
  clientCapabilities: { elicitation: { form: {} } },
  clientInfo: { name: "fixture", version: "1" },
});
process.stdout.write(JSON.stringify(parsed.clientCapabilities.elicitation));
"""
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(SDK_ENTRY)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"form": {}})

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

    def test_question_uses_one_session_and_waits_for_recorded_receipt(self) -> None:
        short_directory = tempfile.TemporaryDirectory(prefix="q-", dir="/tmp")
        self.addCleanup(short_directory.cleanup)
        private = Path(short_directory.name)
        socket_path = private / "q.sock"
        frames: list[dict[str, object]] = []
        errors: list[BaseException] = []
        ready = threading.Event()

        def serve() -> None:
            try:
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(socket_path))
                server.listen(1)
                socket_path.chmod(0o600)
                ready.set()
                connection, _ = server.accept()
                with connection, server:
                    reader = connection.makefile("rb")
                    frames.append(json.loads(reader.readline()))
                    connection.sendall(
                        b'{"kind":"answer","answers":{"question_0_custom":"Use the safe plan."}}\n'
                    )
                    frames.append(json.loads(reader.readline()))
                    connection.sendall(
                        b'{"kind":"recorded","session_id":"fixture-session","tool_call_id":"ask-tool-1"}\n'
                    )
            except (OSError, ValueError, UnicodeError) as error:
                errors.append(error)
                ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        result, log_path = self._run("question", question_socket=socket_path)
        thread.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "output": "question fixture output",
                "session_id": "fixture-session",
                "model": "sonnet",
                "effort": "high",
                "cleanup_confirmed": True,
            },
        )
        self.assertEqual(errors, [])
        self.assertEqual(frames[0]["kind"], "question")
        self.assertEqual(frames[0]["session_id"], "fixture-session")
        self.assertEqual(frames[0]["tool_call_id"], "ask-tool-1")
        self.assertEqual(frames[0]["questions"][0]["field"], "question_0_custom")
        self.assertEqual(
            frames[1],
            {
                "kind": "received",
                "session_id": "fixture-session",
                "tool_call_id": "ask-tool-1",
            },
        )
        calls = self._events(log_path)
        initialize = next(call for call in calls if call["event"] == "initialize")
        self.assertEqual(
            initialize["value"]["clientCapabilities"]["elicitation"],
            {
                "form": {},
            },
        )
        self.assertEqual(
            initialize["value"]["__parsedClientCapabilities"]["elicitation"],
            {"form": {}},
        )
        session_new = next(call for call in calls if call["event"] == "session/new")
        options = session_new["value"]["_meta"]["claudeCode"]["options"]
        self.assertIn("AskUserQuestion", options["tools"])
        self.assertIn("AskUserQuestion", options["allowedTools"])
        self.assertEqual(
            [call["event"] for call in calls if "event" in call].count(
                "session/prompt"
            ),
            1,
        )

    def test_read_only_permission_omits_write_tools(self) -> None:
        result, log_path = self._run(permission="read-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._events(log_path)
        options = calls[1]["value"]["_meta"]["claudeCode"]["options"]
        self.assertEqual(options["tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(options["allowedTools"], ["Read", "Grep", "Glob"])

    def test_explicit_missing_question_socket_fails_before_agent_spawn(self) -> None:
        result, log_path = self._run(question_socket=self.root / "missing.sock")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("question socket", result.stderr)
        self.assertFalse(log_path.exists())

    def test_result_file_and_launch_nonce_are_required_as_a_pair(self) -> None:
        result, log_path = self._run(
            result_file=self.root / "client-result.json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("result-file and launch-nonce", result.stderr)
        self.assertFalse(log_path.exists())

    def test_result_destination_is_validated_before_agent_spawn(self) -> None:
        result, log_path = self._run(
            result_file=self.root / "wrong-name.json",
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("client-result.json", result.stderr)
        self.assertFalse(log_path.exists())

        existing = self.root / "client-result.json"
        existing.write_text("existing\n", encoding="utf-8")
        result, log_path = self._run(
            result_file=existing,
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log_path.exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "existing\n")
        existing.unlink()

        target = self.root / "client-result.json"
        outside = self.root / "outside-result.json"
        outside.write_text("outside\n", encoding="utf-8")
        target.symlink_to(outside)
        result, log_path = self._run(
            result_file=target,
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log_path.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

        target.unlink()
        (self.root / "client-result.pending").write_text("pending\n", encoding="utf-8")
        result, log_path = self._run(
            result_file=target,
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log_path.exists())

        (self.root / "client-result.pending").unlink()
        result, log_path = self._run(
            result_file=target,
            launch_nonce="Planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("launch-nonce", result.stderr)
        self.assertFalse(log_path.exists())

    def test_success_publishes_typed_receipt_before_stdout_result(self) -> None:
        result_file = self.root / "client-result.json"
        result, log_path = self._run(
            result_file=result_file,
            launch_nonce="planner1234",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(log_path.exists())
        envelope = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["launch_nonce"], "planner1234")
        self.assertEqual(
            envelope["receipt"],
            {
                "output": "fixture output",
                "session_id": "fixture-session",
                "model": "sonnet",
                "effort": "high",
                "cleanup_confirmed": True,
            },
        )
        self.assertEqual(json.loads(result.stdout), envelope["receipt"])
        self.assertEqual(result_file.stat().st_mode & 0o777, 0o600)

    def test_directory_fsync_failure_is_exit_two_and_not_a_confirmed_receipt(
        self,
    ) -> None:
        patch = self.root / "fail-directory-fsync.mjs"
        patch.write_text(
            """
import fs from "node:fs";
const original = fs.fsyncSync;
let count = 0;
fs.fsyncSync = (fd) => {
  count += 1;
  if (count === 2) throw new Error("directory fsync failure");
  return original(fd);
};
""",
            encoding="utf-8",
        )
        result_file = self.root / "client-result.json"
        result, _ = self._run(
            result_file=result_file,
            launch_nonce="planner1234",
            client_prefix=("--import", str(patch)),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(result_file.exists())
        self.assertEqual(json.loads(result.stdout)["cleanup_confirmed"], False)
        self.assertIn("directory fsync failure", result.stderr)

    def test_failure_receipt_reports_cleanup_state_and_requested_config(self) -> None:
        result_file = self.root / "client-result.json"
        result, _ = self._run(
            "max-tokens",
            result_file=result_file,
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        envelope = json.loads(result_file.read_text(encoding="utf-8"))
        receipt = envelope["receipt"]
        self.assertEqual(envelope["launch_nonce"], "planner1234")
        self.assertEqual(
            set(receipt),
            {"error", "session_id", "model", "effort", "cleanup_confirmed"},
        )
        self.assertEqual(receipt["session_id"], "fixture-session")
        self.assertEqual(receipt["model"], "sonnet")
        self.assertEqual(receipt["effort"], "high")
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertEqual(json.loads(result.stdout), receipt)

    def test_pre_spawn_socket_failure_can_report_cleanup_confirmed(self) -> None:
        result_file = self.root / "client-result.json"
        result, log_path = self._run(
            question_socket=self.root / "missing.sock",
            result_file=result_file,
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(log_path.exists())
        receipt = json.loads(result_file.read_text(encoding="utf-8"))["receipt"]
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertIsNone(receipt["session_id"])

    def test_failure_receipt_marks_session_close_failure_as_unconfirmed(self) -> None:
        result_file = self.root / "client-result.json"
        result, _ = self._run(
            "session-close-fails",
            result_file=result_file,
            launch_nonce="planner1234",
        )
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads(result_file.read_text(encoding="utf-8"))["receipt"]
        self.assertFalse(receipt["cleanup_confirmed"])

    def test_sigterm_publishes_failure_receipt_after_cleanup(self) -> None:
        result_file = self.root / "client-result.json"
        log_path = self.root / "sigterm-receipt.jsonl"
        args = [
            NODE,
            str(CLIENT),
            "--harness",
            "claude",
            "--sdk-entry",
            str(SDK_ENTRY),
            "--agent-argv",
            json.dumps(
                [
                    NODE,
                    str(self.fixture),
                    str(log_path),
                    "wait",
                    "claude",
                    str(SDK_ENTRY),
                ]
            ),
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
            "--result-file",
            str(result_file),
            "--launch-nonce",
            "planner1234",
        ]
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdin is not None
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
        stdout_receipt = json.loads(stdout)
        self.assertEqual(stdout_receipt["session_id"], "fixture-session")
        self.assertTrue(stdout_receipt["cleanup_confirmed"])
        receipt = json.loads(result_file.read_text(encoding="utf-8"))["receipt"]
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertNotEqual(stderr, "")

    def test_question_channel_failure_aborts_prompt_even_if_agent_ignores_cancel(
        self,
    ) -> None:
        short_directory = tempfile.TemporaryDirectory(prefix="q-", dir="/tmp")
        self.addCleanup(short_directory.cleanup)
        socket_path = Path(short_directory.name) / "q.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        socket_path.chmod(0o600)
        ready = threading.Event()
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _ = server.accept()
                with connection:
                    buffer = b""
                    while b"\n" not in buffer:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        buffer += chunk
                    ready.set()
                    connection.sendall(
                        b'{"kind":"answer","answers":{"wrong":"value"}}\n'
                    )
                    while connection.recv(4096):
                        pass
            except OSError as error:
                errors.append(error)
                ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result, log_path = self._run(
            "question-ignore-cancel",
            question_socket=socket_path,
        )
        thread.join(timeout=5)
        server.close()
        self.assertTrue(ready.is_set())
        self.assertEqual(errors, [])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("question", result.stderr)
        events = [
            entry["event"] for entry in self._events(log_path) if "event" in entry
        ]
        self.assertIn("agent-final-success", events)
        self.assertIn("session/cancel", events)
        self.assertLess(events.index("session/cancel"), events.index("session/close"))

    def test_malformed_known_question_is_fatal_without_opening_question_channel(
        self,
    ) -> None:
        short_directory = tempfile.TemporaryDirectory(prefix="q-", dir="/tmp")
        self.addCleanup(short_directory.cleanup)
        socket_path = Path(short_directory.name) / "q.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        server.settimeout(1)
        socket_path.chmod(0o600)
        ready = threading.Event()
        connected = threading.Event()
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                ready.set()
                connection, _ = server.accept()
                connected.set()
                connection.close()
            except TimeoutError:
                pass
            except OSError as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        result, log_path = self._run(
            "question-malformed",
            question_socket=socket_path,
        )
        thread.join(timeout=3)
        server.close()
        self.assertEqual(errors, [])
        self.assertFalse(connected.is_set())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        events = [
            entry["event"] for entry in self._events(log_path) if "event" in entry
        ]
        self.assertIn("agent-final-success", events)
        self.assertIn("session/cancel", events)
        self.assertLess(events.index("session/cancel"), events.index("session/close"))

    def test_observed_question_without_form_is_fatal(self) -> None:
        short_directory = tempfile.TemporaryDirectory(prefix="q-", dir="/tmp")
        self.addCleanup(short_directory.cleanup)
        socket_path = Path(short_directory.name) / "q.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        server.settimeout(1)
        socket_path.chmod(0o600)
        ready = threading.Event()
        connected = threading.Event()

        def serve() -> None:
            ready.set()
            try:
                connection, _ = server.accept()
                connected.set()
                connection.close()
            except TimeoutError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        result, log_path = self._run("question-no-form", question_socket=socket_path)
        thread.join(timeout=3)
        server.close()
        self.assertFalse(connected.is_set())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        events = [
            entry["event"] for entry in self._events(log_path) if "event" in entry
        ]
        self.assertIn("agent-final-success", events)
        self.assertIn("session/cancel", events)
        self.assertLess(events.index("session/cancel"), events.index("session/close"))

    def test_no_channel_ask_notification_cannot_claim_success(self) -> None:
        result, log_path = self._run("question-no-form")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        events = [
            entry["event"] for entry in self._events(log_path) if "event" in entry
        ]
        self.assertIn("agent-final-success", events)
        self.assertIn("session/cancel", events)
        self.assertLess(events.index("session/cancel"), events.index("session/close"))

    def test_form_pending_recorded_cannot_race_successful_prompt_end(self) -> None:
        short_directory = tempfile.TemporaryDirectory(prefix="q-", dir="/tmp")
        self.addCleanup(short_directory.cleanup)
        socket_path = Path(short_directory.name) / "q.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        socket_path.chmod(0o600)
        ready = threading.Event()
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _ = server.accept()
                with connection:
                    buffer = b""
                    while b"\n" not in buffer:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        buffer += chunk
                    ready.set()
                    while connection.recv(4096):
                        pass
            except OSError as error:
                errors.append(error)
                ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        result, log_path = self._run(
            "question-pending-final", question_socket=socket_path
        )
        thread.join(timeout=5)
        server.close()
        self.assertTrue(ready.is_set())
        self.assertEqual(errors, [])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        events = [
            entry["event"] for entry in self._events(log_path) if "event" in entry
        ]
        self.assertIn("agent-final-success", events)
        self.assertIn("session/cancel", events)
        self.assertLess(events.index("session/cancel"), events.index("session/close"))

    def test_question_socket_is_rejected_for_codex_before_agent_spawn(self) -> None:
        result, log_path = self._run(
            harness="codex",
            question_socket=self.root / "missing.sock",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only available for claude", result.stderr)
        self.assertFalse(log_path.exists())

    def test_codex_uses_its_effort_option_and_omits_claude_metadata(self) -> None:
        self.assertIsNotNone(CODEX_SDK_ENTRY, "AGENT_TEAM_CODEX_SDK_ENTRY is required")
        result, log_path = self._run(harness="codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._events(log_path)
        self.assertEqual(calls[3]["value"]["configId"], "reasoning_effort")
        self.assertEqual(
            calls[1]["value"],
            {"cwd": str(self.workspace.resolve()), "mcpServers": []},
        )
        self.assertEqual(json.loads(result.stdout)["effort"], "high")

    def test_unknown_harness_is_rejected_before_agent_spawn(self) -> None:
        result, log_path = self._run(harness="unselected")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("harness must be claude or codex", result.stderr)
        self.assertFalse(log_path.exists())

    def test_model_and_effort_mismatch_is_rejected_before_prompt(self) -> None:
        for mode in ("model-mismatch", "effort-mismatch", "effort-reroutes-model"):
            with self.subTest(mode=mode):
                result, log_path = self._run(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("selection does not match", result.stderr)
                methods = [entry["event"] for entry in self._events(log_path)]
                self.assertNotIn("session/prompt", methods)
                self.assertEqual(methods[-2:], ["session/close", "stdin-end"])
                self.assertEqual(result.stdout, "")

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
            "--harness",
            "claude",
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

    def test_sigterm_cancels_pending_question_and_closes_socket(self) -> None:
        short_directory = tempfile.TemporaryDirectory(prefix="q-", dir="/tmp")
        self.addCleanup(short_directory.cleanup)
        socket_path = Path(short_directory.name) / "q.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        socket_path.chmod(0o600)
        question_ready = threading.Event()
        socket_closed = threading.Event()
        server_errors: list[BaseException] = []

        def serve() -> None:
            try:
                connection, _ = server.accept()
                with connection:
                    buffer = b""
                    while b"\n" not in buffer:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        buffer += chunk
                    question_ready.set()
                    while connection.recv(4096):
                        pass
                    socket_closed.set()
            except OSError as error:
                server_errors.append(error)
                question_ready.set()
                socket_closed.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        log_path = self.root / "question-cancel.jsonl"
        args = [
            NODE,
            str(CLIENT),
            "--harness",
            "claude",
            "--sdk-entry",
            str(SDK_ENTRY),
            "--agent-argv",
            json.dumps([NODE, str(self.fixture), str(log_path), "question", "claude"]),
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
            "--question-socket",
            str(socket_path),
        ]
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env={**os.environ, "HOME": str(self.home)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdin is not None
            process.stdin.write("fixture prompt")
            process.stdin.close()
            process.stdin = None
            self.assertTrue(question_ready.wait(timeout=10))
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            server.close()
        thread.join(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(stdout, "")
        self.assertNotEqual(stderr, "")
        self.assertEqual(server_errors, [])
        self.assertTrue(socket_closed.is_set())
        events = [
            entry["event"] for entry in self._events(log_path) if "event" in entry
        ]
        self.assertLess(events.index("session/cancel"), events.index("session/close"))
        self.assertIn("session/close", events)


if __name__ == "__main__":
    unittest.main()
