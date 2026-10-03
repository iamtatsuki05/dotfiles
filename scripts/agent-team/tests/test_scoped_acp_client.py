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

// Copilot mode: the agent is the ACP server itself and asks the client for
// every tool permission.  Responses to those requests carry no method.
const copilotScenarioPath = process.argv[6];
const copilotPending = new Map();
let copilotCwd;
let copilotPromptId;
let copilotCancelAnswered = false;
let copilotDeferredClose;
const COPILOT_OPTIONS = [
  { optionId: "allow-once", name: "Allow", kind: "allow_once" },
  { optionId: "allow-always", name: "Always allow", kind: "allow_always" },
  { optionId: "reject-once", name: "Reject", kind: "reject_once" },
  { optionId: "reject-always", name: "Always reject", kind: "reject_always" },
];
if (harness === "copilot") log("env", Object.keys(process.env).sort());

function copilotNotify(update) {
  process.stdout.write(`${JSON.stringify({
    jsonrpc: "2.0",
    method: "session/update",
    params: { sessionId: "copilot-session", update },
  })}\n`);
}

function copilotFinish() {
  copilotNotify({
    sessionUpdate: "agent_message_chunk",
    messageId: "copilot-final",
    content: { type: "text", text: "copilot fixture output" },
  });
  const id = copilotPromptId;
  copilotPromptId = undefined;
  send(id, { stopReason: "end_turn" });
}

function copilotAsk(id, name, params, onAnswer = undefined) {
  copilotPending.set(id, { name, onAnswer });
  process.stdout.write(`${JSON.stringify({
    jsonrpc: "2.0",
    id,
    method: "session/request_permission",
    params,
  })}\n`);
}

function copilotTool(toolCallId, kind, target) {
  return {
    sessionId: "copilot-session",
    toolCall: { toolCallId, kind, title: `${kind} fixture`, locations: [{ path: target }] },
    options: COPILOT_OPTIONS,
  };
}

function copilotScenario(requests, index) {
  if (index >= requests.length) {
    copilotFinish();
    return;
  }
  copilotAsk(5000 + index, requests[index].name, requests[index].params, () =>
    copilotScenario(requests, index + 1),
  );
}

function handleCopilot(message) {
  const method = message.method;
  if (method === undefined) {
    const pending = copilotPending.get(message.id);
    copilotPending.delete(message.id);
    log("permission-response", {
      id: message.id,
      name: pending?.name,
      result: message.result,
      error: message.error,
    });
    pending?.onAnswer?.();
    return;
  }
  log(method, message.params);
  const allowed = () => `${copilotCwd}/src/allowed.txt`;
  if (method === "initialize") {
    send(message.id, {
      protocolVersion: 1,
      agentCapabilities: {
        loadSession: false,
        sessionCapabilities: mode === "copilot-no-close" ? {} : { close: {} },
      },
      agentInfo: {
        name: "github-copilot",
        version: mode === "copilot-version-mismatch" ? "1.0.90" : "1.0.91",
      },
      authMethods: [{ id: "copilot-login", name: "Log in with GitHub" }],
    });
  } else if (method === "session/new") {
    copilotCwd = message.params.cwd;
    if (mode === "copilot-auth-required") {
      sendError(message.id, "Authentication required");
      return;
    }
    send(message.id, {
      sessionId: "copilot-session",
      modes: { currentModeId: "agent", availableModes: [{ id: "agent", name: "Agent" }] },
      models: {
        currentModelId: mode === "copilot-model-mismatch-models" ? "other-model" : "gpt-5.2",
        availableModels: [{ modelId: "gpt-5.2", name: "GPT-5.2" }],
      },
      configOptions: [{
        id: "model",
        type: "select",
        name: "Model",
        currentValue: mode === "copilot-model-mismatch-config" ? "other-model" : "gpt-5.2",
        options: [{ value: "gpt-5.2", name: "GPT-5.2" }],
      }],
    });
  } else if (method === "session/prompt") {
    copilotPromptId = message.id;
    if (mode === "copilot-permissions") {
      copilotScenario(JSON.parse(fs.readFileSync(copilotScenarioPath, "utf8")), 0);
    } else if (mode === "copilot-success") {
      copilotFinish();
    } else if (mode === "copilot-approved-edit") {
      copilotNotify({
        sessionUpdate: "tool_call",
        toolCallId: "edit-approved",
        kind: "edit",
        status: "pending",
        title: "edit fixture",
        locations: [{ path: allowed() }],
      });
      copilotAsk(6000, "edit-approved", copilotTool("edit-approved", "edit", allowed()), () => {
        copilotNotify({ sessionUpdate: "tool_call_update", toolCallId: "edit-approved", status: "in_progress" });
        copilotNotify({ sessionUpdate: "tool_call_update", toolCallId: "edit-approved", status: "completed" });
        copilotFinish();
      });
    } else if (mode === "copilot-unapproved-edit") {
      copilotNotify({
        sessionUpdate: "tool_call",
        toolCallId: "edit-unapproved",
        kind: "edit",
        status: "pending",
        title: "edit fixture",
        locations: [{ path: allowed() }],
      });
      copilotNotify({ sessionUpdate: "tool_call_update", toolCallId: "edit-unapproved", status: "completed" });
    } else if (mode === "copilot-unapproved-execute") {
      copilotNotify({
        sessionUpdate: "tool_call",
        toolCallId: "shell-1",
        kind: "execute",
        status: "in_progress",
        title: "touch canary",
      });
    } else if (mode === "copilot-out-of-scope-read") {
      copilotNotify({
        sessionUpdate: "tool_call",
        toolCallId: "read-outside",
        kind: "read",
        status: "completed",
        title: "read outside",
        locations: [{ path: `${copilotCwd.replace(/\/[^/]+$/, "")}/outside/outside.txt` }],
      });
    } else if (mode === "copilot-mode-change") {
      copilotNotify({ sessionUpdate: "current_mode_update", currentModeId: "autopilot" });
    } else if (mode === "copilot-model-change") {
      copilotNotify({
        sessionUpdate: "config_option_update",
        configOptions: [{
          id: "model",
          type: "select",
          name: "Model",
          currentValue: "other-model",
          options: [{ value: "other-model", name: "Other" }],
        }],
      });
    } else if (mode === "copilot-untitled-execute") {
      copilotNotify({ sessionUpdate: "tool_call", toolCallId: "shell-2", kind: "execute", status: "in_progress" });
    } else if (mode === "copilot-unknown-kind") {
      copilotNotify({
        sessionUpdate: "tool_call",
        toolCallId: "shell-3",
        kind: "shell",
        status: "in_progress",
        title: "touch canary",
      });
    } else if (mode === "copilot-invalid-json") {
      process.stdout.write("not json\n");
    } else if (mode === "copilot-update-without-jsonrpc" || mode === "copilot-update-with-id") {
      const update = {
        method: "session/update",
        params: {
          sessionId: "copilot-session",
          update: {
            sessionUpdate: "tool_call",
            toolCallId: "shell-4",
            kind: "execute",
            status: "in_progress",
            title: "touch canary",
          },
        },
      };
      if (mode === "copilot-update-with-id") Object.assign(update, { jsonrpc: "2.0", id: 77 });
      process.stdout.write(`${JSON.stringify(update)}\n`);
    } else if (mode === "copilot-other-session-update") {
      process.stdout.write(`${JSON.stringify({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId: "other-session",
          update: {
            sessionUpdate: "tool_call",
            toolCallId: "shell-5",
            kind: "execute",
            status: "in_progress",
            title: "touch canary",
          },
        },
      })}\n`);
    } else if (mode === "copilot-approved-edit-drift") {
      copilotAsk(6001, "edit-approved", copilotTool("edit-drift", "edit", allowed()), () => {
        copilotNotify({
          sessionUpdate: "tool_call_update",
          toolCallId: "edit-drift",
          kind: "edit",
          status: "completed",
          locations: [{ path: `${copilotCwd}/src/forbidden.txt` }],
        });
      });
    } else if (mode === "copilot-late-violation") {
      copilotFinish();
      copilotNotify({
        sessionUpdate: "tool_call",
        toolCallId: "edit-late",
        kind: "edit",
        status: "completed",
        title: "late edit",
        locations: [{ path: allowed() }],
      });
    } else if (mode === "copilot-wait") {
      copilotAsk(7000, "before-signal", copilotTool("read-before", "read", allowed()));
    }
  } else if (method === "session/cancel") {
    if (mode === "copilot-wait") {
      copilotAsk(7001, "after-signal", copilotTool("read-after", "read", allowed()), () => {
        copilotCancelAnswered = true;
        if (copilotDeferredClose !== undefined) send(copilotDeferredClose, {});
      });
    }
    if (copilotPromptId !== undefined) {
      send(copilotPromptId, { stopReason: "cancelled" });
      copilotPromptId = undefined;
    }
  } else if (method === "session/close") {
    if (mode === "copilot-wait" && !copilotCancelAnswered) {
      copilotDeferredClose = message.id;
    } else {
      send(message.id, {});
    }
  } else if (method !== "$/cancel_request") {
    sendError(message.id, `unexpected method: ${method}`);
  }
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
    if (harness === "copilot") {
      handleCopilot(message);
      continue;
    }
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


POLICY = ROOT / "agent_team" / "scoped_policy.mjs"
DECIDE_SCRIPT = """
const policy = await import(process.argv[1]);
const [rawPolicy, requests, context] = JSON.parse(process.argv[2]);
const parsed = policy.parsePolicy(rawPolicy);
const decisions = requests.map((request) => policy.decideCopilotPermission(parsed, request, context));
let contextError = "";
try {
  policy.decideCopilotPermission(parsed, requests[0], {});
} catch (error) {
  contextError = error.message;
}
process.stdout.write(JSON.stringify({ decisions, contextError }));
"""


@unittest.skipUnless(NODE, "Node is required")
class CopilotPermissionPolicyTest(unittest.TestCase):
    def test_decision_reports_kind_reason_targets_and_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            (workspace / "src").mkdir(parents=True)
            allowed = workspace / "src" / "allowed.txt"
            allowed.write_text("allowed\n", encoding="utf-8")
            protected = root / "private"
            protected.mkdir()
            options = [
                {"optionId": "once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "no", "name": "Reject", "kind": "reject_once"},
            ]

            def request(kind: str) -> dict[str, object]:
                return {
                    "sessionId": "session-1",
                    "toolCall": {
                        "toolCallId": f"{kind}-1",
                        "kind": kind,
                        "locations": [{"path": str(allowed)}],
                    },
                    "options": options,
                }

            decisions: dict[str, list[dict[str, object]]] = {}
            context_errors: dict[str, str] = {}
            for permission in ("read-only", "workspace-write"):
                policy = {
                    "permission": permission,
                    "workspace": str(workspace),
                    "allowed_paths": ["src/allowed.txt"],
                    "forbidden_paths": [],
                    "protected_paths": [str(protected)],
                }
                assert NODE is not None
                result = subprocess.run(
                    [
                        NODE,
                        "--input-type=module",
                        "-e",
                        DECIDE_SCRIPT,
                        str(POLICY),
                        json.dumps(
                            [
                                policy,
                                [request("read"), request("edit")],
                                {"sessionId": "session-1"},
                            ]
                        ),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                decisions[permission] = payload["decisions"]
                context_errors[permission] = payload["contextError"]

        read, edit = decisions["workspace-write"]
        self.assertEqual(
            read,
            {
                "decision": "allow",
                "reason": "read targets are inside the policy",
                "kind": "read",
                "toolCallId": "read-1",
                "paths": [str(allowed)],
                "response": {"outcome": {"outcome": "selected", "optionId": "once"}},
            },
        )
        self.assertEqual(edit["decision"], "allow")
        self.assertEqual(decisions["read-only"][0]["decision"], "allow")
        self.assertEqual(
            decisions["read-only"][1],
            {
                "decision": "reject",
                "reason": "edit requires the Worker policy",
                "kind": "edit",
                "toolCallId": "edit-1",
                "paths": [],
                "response": {"outcome": {"outcome": "selected", "optionId": "no"}},
            },
        )
        self.assertEqual(
            context_errors["read-only"],
            "scoped ACP: Copilot permission decisions require the active session id",
        )


COPILOT_OPTIONS = [
    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
    {"optionId": "reject-always", "name": "Always reject", "kind": "reject_always"},
]
COPILOT_ENV_KEYS = ["COPILOT_HOME", "HOME", "LANG", "LC_ALL", "LOGNAME", "PATH"]
COPILOT_ENV_KEYS += ["TMPDIR", "USER"]
_MISSING = object()


@unittest.skipUnless(
    NODE and CODEX_SDK_ENTRY is not None,
    "Node and AGENT_TEAM_CODEX_SDK_ENTRY for ACP SDK 1.4.0 are required",
)
class ScopedCopilotAcpClientTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="agent-team-copilot-client-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / ".git" / "hooks").mkdir(parents=True)
        (self.workspace / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        for name in ("src/allowed.txt", "src/forbidden.txt", "README.md"):
            (self.workspace / name).write_text(f"{name}\n", encoding="utf-8")
        self.outside = self.root / "outside" / "outside.txt"
        self.outside.parent.mkdir()
        self.outside.write_text("OUTSIDE-secret\n", encoding="utf-8")
        (self.workspace / "link-out").symlink_to(Path("..") / "outside" / "outside.txt")
        self.home = self.root / "home"
        self.home.mkdir()
        self.fixture = self.root / "fixture-agent.mjs"
        self.fixture.write_text(textwrap.dedent(FIXTURE), encoding="utf-8")

    def _private(self, name: str, permission: str) -> Path:
        private = self.root / name
        private.mkdir(mode=0o700)
        for child in ("copilot-home", "tmp"):
            (private / child).mkdir(mode=0o700)
        policy = private / "write-policy.json"
        policy.write_text(
            json.dumps(
                {
                    "permission": permission,
                    "workspace": str(self.workspace),
                    "allowed_paths": (
                        ["src/allowed.txt"] if permission == "workspace-write" else []
                    ),
                    "forbidden_paths": (
                        ["src/forbidden.txt"] if permission == "workspace-write" else []
                    ),
                    "protected_paths": [str(private)],
                }
            ),
            encoding="utf-8",
        )
        policy.chmod(0o600)
        return private

    def _run(
        self,
        mode: str,
        *,
        permission: str = "workspace-write",
        scenario: list[dict[str, object]] | None = None,
        result_file: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        private = self._private(f"private-{mode}-{permission}", permission)
        log_path = self.root / f"{mode}-{permission}.jsonl"
        scenario_path = self.root / f"{mode}-{permission}-scenario.json"
        scenario_path.write_text(json.dumps(scenario or []), encoding="utf-8")
        args = self._args(
            mode,
            private,
            log_path,
            scenario_path,
            permission=permission,
            result_file=result_file,
        )
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=self._environment(private),
            check=False,
            input="fixture prompt",
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, log_path, private

    def _args(
        self,
        mode: str,
        private: Path,
        log_path: Path,
        scenario_path: Path,
        *,
        permission: str,
        result_file: bool,
    ) -> list[str]:
        assert NODE is not None
        agent_argv = [
            NODE,
            str(self.fixture),
            str(log_path),
            mode,
            "copilot",
            str(CODEX_SDK_ENTRY),
            str(scenario_path),
        ]
        return [
            NODE,
            str(CLIENT),
            "--harness",
            "copilot",
            "--sdk-entry",
            str(CODEX_SDK_ENTRY),
            "--agent-argv",
            json.dumps(agent_argv),
            "--cwd",
            str(self.workspace),
            "--permission",
            permission,
            "--model",
            "gpt-5.2",
            "--effort",
            "high",
            "--instructions",
            "fixture instructions",
            "--timeout-ms",
            "15000",
            "--policy",
            str(private / "write-policy.json"),
            *(
                [
                    "--result-file",
                    str(private / "client-result.json"),
                    "--launch-nonce",
                    "reviewer1234",
                ]
                if result_file
                else []
            ),
        ]

    def _environment(self, private: Path) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(private / "tmp"),
            "COPILOT_HOME": str(private / "copilot-home"),
            "USER": "fixture-user",
            "LOGNAME": "fixture-user",
            "LANG": "C",
            "LC_ALL": "C",
            "SHELL": "/bin/zsh",
            "TERM": "dumb",
            "GH_TOKEN": "gho_fixture",
            "GITHUB_TOKEN": "ghp_fixture",
            "COPILOT_GITHUB_TOKEN": "github_pat_fixture",
            "COPILOT_PROVIDER_API_KEY": "byok-fixture",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "NODE_OPTIONS": "--no-deprecation",
            "XDG_CONFIG_HOME": str(self.home / "xdg"),
        }

    def _events(self, log_path: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
        ]

    def _audit(self, stderr: str) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in stderr.splitlines()
            if line.startswith('{"event":"copilot-permission"')
        ]

    def _request(
        self,
        tool_call_id: object,
        kind: object,
        locations: object,
        *,
        options: object = None,
        raw_input: object = _MISSING,
        session: str = "copilot-session",
        tool_call: object = _MISSING,
    ) -> dict[str, object]:
        call: dict[str, object] = {"toolCallId": tool_call_id, "title": "fixture"}
        if kind is not _MISSING:
            call["kind"] = kind
        if locations is not _MISSING:
            call["locations"] = locations
        if raw_input is not _MISSING:
            call["rawInput"] = raw_input
        return {
            "sessionId": session,
            "toolCall": call if tool_call is _MISSING else tool_call,
            "options": COPILOT_OPTIONS if options is None else options,
        }

    def _decisions(
        self,
        rows: list[tuple[str, dict[str, object], dict[str, object]]],
        **kwargs: str,
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        scenario = [{"name": name, "params": params} for name, params, _ in rows]
        result, log_path, _ = self._run(
            "copilot-permissions", scenario=scenario, **kwargs
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [
            event["value"]
            for event in self._events(log_path)
            if event["event"] == "permission-response"
        ]
        self.assertEqual(
            [response["name"] for response in responses], [name for name, *_ in rows]
        )
        for (name, _params, expected), response in zip(rows, responses, strict=True):
            with self.subTest(row=name):
                self.assertEqual(response["result"], {"outcome": expected})
        return result, responses

    @staticmethod
    def _selected(option_id: str) -> dict[str, object]:
        return {"outcome": "selected", "optionId": option_id}

    def test_session_is_closed_policy_bound_and_never_configured(self) -> None:
        result, log_path, _ = self._run("copilot-success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "output": "copilot fixture output",
                "session_id": "copilot-session",
                "model": "gpt-5.2",
                "effort": "high",
                "cleanup_confirmed": True,
            },
        )
        events = self._events(log_path)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "env",
                "initialize",
                "session/new",
                "session/prompt",
                "session/close",
                "stdin-end",
            ],
        )
        # macOS CoreFoundation adds this key inside every Node process, even one
        # started with an empty environment, so it is not inherited from the client.
        self.assertEqual(
            sorted(set(events[0]["value"]) - {"__CF_USER_TEXT_ENCODING"}),
            sorted(COPILOT_ENV_KEYS),
        )
        self.assertEqual(
            events[1]["value"]["clientCapabilities"],
            {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        )
        self.assertEqual(
            events[2]["value"], {"cwd": str(self.workspace), "mcpServers": []}
        )
        self.assertEqual(
            events[3]["value"]["prompt"],
            [{"type": "text", "text": "fixture instructions\n\nfixture prompt"}],
        )

    def test_worker_permissions_follow_the_decision_table(self) -> None:
        ws = str(self.workspace)
        allowed = f"{ws}/src/allowed.txt"
        readme = f"{ws}/README.md"
        outside = str(self.outside)
        allow = self._selected("allow-once")
        reject = self._selected("reject-once")
        rows: list[tuple[str, dict[str, object], dict[str, object]]] = [
            ("read-allowed", self._request("r1", "read", [{"path": allowed}]), allow),
            ("read-readme", self._request("r2", "read", [{"path": readme}]), allow),
            ("search-root", self._request("r3", "search", [{"path": ws}]), allow),
            (
                "read-raw-input-located",
                self._request(
                    "r4",
                    "read",
                    [{"path": allowed, "line": 3}],
                    raw_input={"path": allowed},
                ),
                allow,
            ),
            ("edit-allowed", self._request("e1", "edit", [{"path": allowed}]), allow),
            (
                "edit-raw-input-file-path",
                self._request(
                    "e8", "edit", [{"path": allowed}], raw_input={"file_path": allowed}
                ),
                allow,
            ),
            (
                "raw-input-unverified-field",
                self._request(
                    "u1",
                    "read",
                    [{"path": allowed}],
                    raw_input={"path": allowed, "view_range": [1, 2]},
                ),
                reject,
            ),
            (
                "search-pattern-escape",
                self._request(
                    "u2", "search", [{"path": ws}], raw_input={"pattern": "../**"}
                ),
                reject,
            ),
            (
                "search-extra-paths",
                self._request(
                    "u3", "search", [{"path": ws}], raw_input={"paths": [outside]}
                ),
                reject,
            ),
            (
                "edit-content-field",
                self._request(
                    "u4",
                    "edit",
                    [{"path": allowed}],
                    raw_input={"path": allowed, "new_str": "changed"},
                ),
                reject,
            ),
            ("read-outside", self._request("r5", "read", [{"path": outside}]), reject),
            (
                "read-dotdot",
                self._request("r6", "read", [{"path": f"{ws}/../outside/outside.txt"}]),
                reject,
            ),
            (
                "read-git",
                self._request("r7", "read", [{"path": f"{ws}/.git/config"}]),
                reject,
            ),
            (
                "read-symlink",
                self._request("r8", "read", [{"path": f"{ws}/link-out"}]),
                reject,
            ),
            (
                "edit-forbidden",
                self._request("e2", "edit", [{"path": f"{ws}/src/forbidden.txt"}]),
                reject,
            ),
            (
                "edit-out-of-scope",
                self._request("e3", "edit", [{"path": readme}]),
                reject,
            ),
            (
                "edit-new-out-of-scope",
                self._request("e4", "edit", [{"path": f"{ws}/src/new.txt"}]),
                reject,
            ),
            (
                "edit-git-hook",
                self._request("e5", "edit", [{"path": f"{ws}/.git/hooks/pre-commit"}]),
                reject,
            ),
            ("edit-outside", self._request("e6", "edit", [{"path": outside}]), reject),
            (
                "read-one-target-invalid",
                self._request("r9", "read", [{"path": allowed}, {"path": outside}]),
                reject,
            ),
            (
                "edit-one-target-invalid",
                self._request("e7", "edit", [{"path": allowed}, {"path": readme}]),
                reject,
            ),
            (
                "location-relative",
                self._request("l1", "read", [{"path": "src/allowed.txt"}]),
                reject,
            ),
            (
                "location-extra-key",
                self._request("l2", "read", [{"path": allowed, "mode": "w"}]),
                reject,
            ),
            ("location-not-object", self._request("l3", "read", [allowed]), reject),
            (
                "location-negative-line",
                self._request("l4", "read", [{"path": allowed, "line": -1}]),
                reject,
            ),
            (
                "locations-not-array",
                self._request("l5", "read", {"path": allowed}),
                reject,
            ),
            (
                "raw-input-not-located",
                self._request(
                    "x1", "read", [{"path": allowed}], raw_input={"path": readme}
                ),
                reject,
            ),
            (
                "raw-input-relative",
                self._request(
                    "x2",
                    "edit",
                    [{"path": allowed}],
                    raw_input={"path": "src/allowed.txt"},
                ),
                reject,
            ),
            (
                "raw-input-unknown-shape",
                self._request("x3", "read", [{"path": allowed}], raw_input=allowed),
                reject,
            ),
            (
                "raw-input-only",
                self._request("x4", "read", _MISSING, raw_input={"path": allowed}),
                reject,
            ),
            ("no-targets", self._request("x5", "search", _MISSING), reject),
            (
                "allow-always-and-reject",
                self._request(
                    "o1",
                    "read",
                    [{"path": allowed}],
                    options=[COPILOT_OPTIONS[1], COPILOT_OPTIONS[2]],
                ),
                reject,
            ),
            (
                "allow-always-only",
                self._request(
                    "o2", "read", [{"path": allowed}], options=[COPILOT_OPTIONS[1]]
                ),
                {"outcome": "cancelled"},
            ),
            (
                "reject-always-only",
                self._request(
                    "o3",
                    "execute",
                    [{"path": allowed}],
                    options=[COPILOT_OPTIONS[0], COPILOT_OPTIONS[3]],
                ),
                self._selected("reject-always"),
            ),
            *(
                (
                    f"kind-{kind}",
                    self._request(f"k-{kind}", kind, [{"path": allowed}]),
                    reject,
                )
                for kind in (
                    "execute",
                    "fetch",
                    "delete",
                    "move",
                    "think",
                    "switch_mode",
                    "other",
                    "teleport",
                )
            ),
            (
                "kind-missing",
                self._request("k-missing", _MISSING, [{"path": allowed}]),
                reject,
            ),
            (
                "session-mismatch",
                self._request(
                    "s1", "read", [{"path": allowed}], session="other-session"
                ),
                reject,
            ),
            (
                "tool-call-id-empty",
                self._request("", "read", [{"path": allowed}]),
                reject,
            ),
            (
                "tool-call-id-long",
                self._request("t" * 257, "read", [{"path": allowed}]),
                reject,
            ),
            (
                "tool-call-not-object",
                self._request("t1", "read", [], tool_call="read"),
                reject,
            ),
        ]
        result, responses = self._decisions(rows)
        self.assertNotIn(
            "allow-always",
            json.dumps([response["result"] for response in responses]),
        )
        audit = self._audit(result.stderr)
        self.assertEqual(len(audit), len(rows))
        self.assertEqual(
            [entry["decision"] for entry in audit],
            [
                "allow" if expected == self._selected("allow-once") else "reject"
                for _name, _params, expected in rows
            ],
        )
        self.assertEqual(audit[0]["paths"], ["src/allowed.txt"])
        self.assertEqual(audit[2]["paths"], ["."])
        self.assertEqual(json.loads(result.stdout)["output"], "copilot fixture output")

    def test_reviewer_permissions_reject_every_edit(self) -> None:
        ws = str(self.workspace)
        allowed = f"{ws}/src/allowed.txt"
        rows: list[tuple[str, dict[str, object], dict[str, object]]] = [
            (
                "read-allowed",
                self._request("r1", "read", [{"path": allowed}]),
                self._selected("allow-once"),
            ),
            (
                "search-root",
                self._request("r2", "search", [{"path": ws}]),
                self._selected("allow-once"),
            ),
            (
                "edit-allowed",
                self._request("e1", "edit", [{"path": allowed}]),
                self._selected("reject-once"),
            ),
            (
                "read-git",
                self._request("r3", "read", [{"path": f"{ws}/.git/config"}]),
                self._selected("reject-once"),
            ),
        ]
        result, _ = self._decisions(rows, permission="read-only")
        self.assertIn("edit requires the Worker policy", result.stderr)

    def test_approved_edit_may_complete(self) -> None:
        result, log_path, _ = self._run("copilot-approved-edit")
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [
            event["value"]
            for event in self._events(log_path)
            if event["event"] == "permission-response"
        ]
        self.assertEqual(
            responses[0]["result"],
            {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
        )

    def test_unapproved_activity_and_mode_changes_are_fatal(self) -> None:
        for mode, message in (
            ("copilot-unapproved-edit", "an unapproved edit tool call ran"),
            ("copilot-unapproved-execute", "an unapproved execute tool call ran"),
            (
                "copilot-out-of-scope-read",
                "a read tool call completed outside the policy",
            ),
            ("copilot-mode-change", "the session mode changed"),
            ("copilot-model-change", "the session model changed"),
            (
                "copilot-approved-edit-drift",
                "an approved edit reported a location outside its approval",
            ),
            (
                "copilot-unknown-kind",
                "a tool call reported an unknown kind or status",
            ),
            (
                "copilot-untitled-execute",
                "the agent sent a session update that cannot be inspected",
            ),
            ("copilot-invalid-json", "the agent sent a message that is not JSON"),
            (
                "copilot-update-without-jsonrpc",
                "the agent sent a message that is not JSON-RPC 2.0",
            ),
            (
                "copilot-update-with-id",
                "the agent sent a session update that cannot be inspected",
            ),
            (
                "copilot-other-session-update",
                "a session update named another session",
            ),
        ):
            with self.subTest(mode=mode):
                result, log_path, private = self._run(mode, result_file=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                receipt = json.loads(
                    (private / "client-result.json").read_text(encoding="utf-8")
                )["receipt"]
                self.assertEqual(
                    receipt["error"],
                    f"scoped Copilot ACP policy violation: {message}",
                )
                self.assertTrue(receipt["cleanup_confirmed"])
                events = [event["event"] for event in self._events(log_path)]
                self.assertLess(
                    events.index("session/cancel"), events.index("session/close")
                )
                self.assertEqual(events[-1], "stdin-end")

    def test_violation_after_end_turn_still_fails_the_turn(self) -> None:
        result, log_path, private = self._run(
            "copilot-late-violation", result_file=True
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        receipt = json.loads(
            (private / "client-result.json").read_text(encoding="utf-8")
        )["receipt"]
        self.assertEqual(
            receipt["error"],
            "scoped Copilot ACP policy violation: an unapproved edit tool call ran",
        )
        self.assertNotIn("output", receipt)
        self.assertTrue(receipt["cleanup_confirmed"])
        events = [event["event"] for event in self._events(log_path)]
        self.assertEqual(events[-2:], ["session/close", "stdin-end"])

    def test_agent_identity_and_model_are_checked_before_any_prompt(self) -> None:
        for mode, message, session_created in (
            (
                "copilot-version-mismatch",
                "Copilot ACP agent version 1.0.90 is not 1.0.91",
                False,
            ),
            (
                "copilot-no-close",
                "Copilot ACP agent does not advertise session/close",
                False,
            ),
            (
                "copilot-model-mismatch-models",
                "Copilot ACP session model does not match requested model",
                True,
            ),
            (
                "copilot-model-mismatch-config",
                "Copilot ACP session model does not match requested model",
                True,
            ),
        ):
            with self.subTest(mode=mode):
                result, log_path, private = self._run(mode, result_file=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                receipt = json.loads(
                    (private / "client-result.json").read_text(encoding="utf-8")
                )["receipt"]
                self.assertIn(message, receipt["error"])
                self.assertTrue(receipt["cleanup_confirmed"])
                events = [event["event"] for event in self._events(log_path)]
                self.assertNotIn("session/prompt", events)
                self.assertNotIn("session/set_config_option", events)
                self.assertEqual("session/close" in events, session_created)

    def test_session_start_failure_is_reported_without_login(self) -> None:
        result, log_path, private = self._run("copilot-auth-required", result_file=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        receipt = json.loads(
            (private / "client-result.json").read_text(encoding="utf-8")
        )["receipt"]
        # A failed session/new cannot prove that no session exists, so the shared
        # cleanup rule reports it as unconfirmed after the typed start failure.
        self.assertEqual(
            receipt["error"],
            "Copilot ACP session could not start: Authentication required; "
            "agent-team does not log in, copy credentials, or fall back to another "
            "provider; cleanup unconfirmed",
        )
        self.assertFalse(receipt["cleanup_confirmed"])
        events = [event["event"] for event in self._events(log_path)]
        self.assertEqual(events, ["env", "initialize", "session/new", "stdin-end"])

    def test_sigterm_cancels_later_permission_requests_then_closes(self) -> None:
        # The client answers each request synchronously, so "pending" here means
        # a request the agent sends after the signal, while it is cancelling.
        private = self._private("private-wait", "read-only")
        log_path = self.root / "wait.jsonl"
        scenario_path = self.root / "wait-scenario.json"
        scenario_path.write_text("[]", encoding="utf-8")
        process = subprocess.Popen(
            self._args(
                "copilot-wait",
                private,
                log_path,
                scenario_path,
                permission="read-only",
                result_file=True,
            ),
            cwd=ROOT,
            env=self._environment(private),
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
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if log_path.exists() and '"before-signal"' in log_path.read_text(
                    encoding="utf-8"
                ):
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            else:
                self.fail("fixture did not receive the first permission decision")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        self.assertEqual(process.returncode, 1, stderr)
        receipt = json.loads((private / "client-result.json").read_text("utf-8"))[
            "receipt"
        ]
        self.assertEqual(json.loads(stdout), receipt)
        self.assertIn("received SIGTERM", receipt["error"])
        self.assertTrue(receipt["cleanup_confirmed"])
        self.assertEqual(receipt["session_id"], "copilot-session")
        events = self._events(log_path)
        names = [event["event"] for event in events]
        responses = {
            event["value"]["name"]: event["value"]["result"]
            for event in events
            if event["event"] == "permission-response"
        }
        self.assertEqual(
            responses,
            {
                "before-signal": {
                    "outcome": {"outcome": "selected", "optionId": "allow-once"}
                },
                "after-signal": {"outcome": {"outcome": "cancelled"}},
            },
        )
        self.assertLess(names.index("session/cancel"), names.index("session/close"))
        self.assertEqual(names[-1], "stdin-end")

    def test_policy_is_required_and_must_match_the_role(self) -> None:
        private = self._private("private-mismatch", "read-only")
        log_path = self.root / "mismatch.jsonl"
        scenario_path = self.root / "mismatch-scenario.json"
        scenario_path.write_text("[]", encoding="utf-8")
        args = self._args(
            "copilot-success",
            private,
            log_path,
            scenario_path,
            permission="workspace-write",
            result_file=False,
        )
        cases = (
            ("permission", args, "policy permission does not match"),
            (
                "missing-policy",
                args[: args.index("--policy")] + args[args.index("--policy") + 2 :],
                "--policy is required for copilot",
            ),
        )
        for name, argv, message in cases:
            with self.subTest(case=name):
                result = subprocess.run(
                    argv,
                    cwd=ROOT,
                    env=self._environment(private),
                    check=False,
                    input="fixture prompt",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(log_path.exists())

        environment = self._environment(private)
        del environment["COPILOT_HOME"]
        read_only = self._args(
            "copilot-success",
            private,
            log_path,
            scenario_path,
            permission="read-only",
            result_file=False,
        )
        result = subprocess.run(
            read_only,
            cwd=ROOT,
            env=environment,
            check=False,
            input="fixture prompt",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Copilot environment requires an absolute COPILOT_HOME", result.stderr
        )
        self.assertFalse(log_path.exists())


if __name__ == "__main__":
    unittest.main()
