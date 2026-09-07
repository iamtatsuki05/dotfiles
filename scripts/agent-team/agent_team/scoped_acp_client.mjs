#!/usr/bin/env node

import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import { fileURLToPath, pathToFileURL } from "node:url";

const READ_TOOLS = Object.freeze(["Read", "Grep", "Glob"]);
const WRITE_TOOLS = Object.freeze(["Write", "Edit"]);
const WORKSPACE_TOOLS = Object.freeze([...READ_TOOLS, ...WRITE_TOOLS]);
const READ_TOOL_NAMES = new Set(READ_TOOLS.map((name) => name.toLowerCase()));
const CLIENT_NAME = "agent-team-scoped-acp-client";
const CLIENT_VERSION = "1";
const CLEANUP_TIMEOUT_MS = 2_000;
const CHILD_EXIT_TIMEOUT_MS = 2_000;
const SDK_PACKAGE = "@agentclientprotocol/sdk";
const SDK_VERSIONS = Object.freeze({ claude: "1.3.0", codex: "1.4.0" });

function fail(message) {
  throw new Error(`scoped ACP client: ${message}`);
}

function assertString(value, field) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    fail(`${field} must be a non-empty string without NUL`);
  }
  return value;
}

function absolutePath(value, field) {
  const raw = assertString(value, field);
  if (!path.isAbsolute(raw)) fail(`${field} must be absolute`);
  const resolved = path.resolve(raw);
  let stat;
  try {
    stat = fs.lstatSync(resolved);
  } catch (error) {
    fail(`${field} is unavailable: ${error?.message ?? error}`);
  }
  if (stat.isSymbolicLink()) fail(`${field} must not be a symlink`);
  return fs.realpathSync.native(resolved);
}

function existingDirectory(value, field) {
  const resolved = absolutePath(value, field);
  if (!fs.statSync(resolved).isDirectory()) fail(`${field} must be a directory`);
  return resolved;
}

function parsePositiveInteger(value, field) {
  const raw = assertString(value, field);
  if (!/^[1-9]\d*$/.test(raw)) fail(`${field} must be a positive integer`);
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) fail(`${field} is out of range`);
  return parsed;
}

function parsePermission(value) {
  if (value !== "read-only" && value !== "workspace-write") {
    fail("permission must be read-only or workspace-write");
  }
  return value;
}

function parseHarness(value) {
  if (value !== "claude" && value !== "codex") fail("harness must be claude or codex");
  return value;
}

export function parseCliArgs(argv) {
  const values = {};
  const known = new Set([
    "--harness",
    "--sdk-entry",
    "--agent-argv",
    "--cwd",
    "--permission",
    "--model",
    "--effort",
    "--instructions",
    "--timeout-ms",
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!known.has(key)) fail(`unknown option: ${String(key)}`);
    if (values[key] !== undefined) fail(`duplicate option: ${key}`);
    const value = argv[index + 1];
    if (value === undefined) fail(`${key} requires a value`);
    values[key] = value;
    index += 1;
  }
  for (const key of [
    "--harness",
    "--sdk-entry",
    "--cwd",
    "--permission",
    "--model",
    "--effort",
    "--instructions",
    "--timeout-ms",
  ]) {
    if (values[key] === undefined) fail(`${key} is required`);
  }
  if (values["--agent-argv"] === undefined) fail("--agent-argv is required");
  let agentArgv;
  try {
    agentArgv = JSON.parse(values["--agent-argv"]);
  } catch (error) {
    fail(`agent argv JSON is invalid: ${error?.message ?? error}`);
  }
  if (!Array.isArray(agentArgv) || agentArgv.length === 0 || agentArgv.some((item) => typeof item !== "string" || item.length === 0 || item.includes("\0"))) {
    fail("agent argv must be a non-empty array of strings without NUL");
  }
  return {
    harness: parseHarness(values["--harness"]),
    sdkEntry: absolutePath(values["--sdk-entry"], "SDK entry"),
    agentArgv,
    cwd: existingDirectory(values["--cwd"], "cwd"),
    permission: parsePermission(values["--permission"]),
    model: assertString(values["--model"], "model"),
    effort: assertString(values["--effort"], "effort"),
    instructions: assertString(values["--instructions"], "instructions"),
    timeoutMs: parsePositiveInteger(values["--timeout-ms"], "timeout-ms"),
  };
}

function validateSdkEntry(entry, version) {
  if (path.basename(entry) !== "acp.js" || path.basename(path.dirname(entry)) !== "dist") {
    fail("SDK entry must be @agentclientprotocol/sdk/dist/acp.js");
  }
  let current = path.dirname(entry);
  while (true) {
    const manifestPath = path.join(current, "package.json");
    if (fs.existsSync(manifestPath)) {
      let manifest;
      try {
        manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
      } catch (error) {
        fail(`SDK package manifest is invalid: ${error?.message ?? error}`);
      }
      if (manifest?.name === SDK_PACKAGE && manifest?.version === version) {
        return entry;
      }
    }
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  fail(`SDK entry must belong to ${SDK_PACKAGE}@${version}`);
}

async function loadSdk(sdkEntry, harness) {
  const version = SDK_VERSIONS[parseHarness(harness)];
  validateSdkEntry(sdkEntry, version);
  const sdk = await import(pathToFileURL(sdkEntry).href);
  if (typeof sdk.client !== "function" || typeof sdk.ndJsonStream !== "function") {
    fail(`installed ${SDK_PACKAGE}@${version} does not expose client and ndJsonStream`);
  }
  return sdk;
}

function fixedTools(permission) {
  return permission === "workspace-write" ? [...WORKSPACE_TOOLS] : [...READ_TOOLS];
}

export function buildSessionRequest(options) {
  if (parseHarness(options.harness) === "codex") {
    return { cwd: options.cwd, mcpServers: [] };
  }
  const tools = fixedTools(options.permission);
  const claudeOptions = {
    model: options.model,
    tools,
    allowedTools: [...tools],
  };
  return {
    cwd: options.cwd,
    mcpServers: [],
    _meta: {
      systemPrompt: { append: options.instructions },
      claudeCode: {
        options: claudeOptions,
      },
    },
  };
}

function requireConfigValue(response, id, label) {
  if (!Array.isArray(response?.configOptions)) {
    fail(`${label} response omitted configOptions`);
  }
  const option = response.configOptions.find((item) => item && item.id === id);
  if (!option || typeof option.currentValue !== "string" || option.currentValue.length === 0) {
    fail(`${label} response omitted selected config value`);
  }
  return option.currentValue;
}

function textFromUpdate(update) {
  if (!update || update.sessionUpdate !== "agent_message_chunk") return "";
  const content = update.content;
  const blocks = Array.isArray(content) ? content : [content];
  return blocks
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("");
}

function permissionIsReadOnly(request) {
  const toolCall = request?.toolCall;
  const candidates = [toolCall?.title, toolCall?.name, toolCall?.kind]
    .filter((value) => typeof value === "string")
    .map((value) => value.toLowerCase());
  return candidates.some((value) => [...READ_TOOL_NAMES].some((tool) => value.includes(tool)));
}

function selectPermission(request) {
  const options = Array.isArray(request?.options) ? request.options : [];
  const allow = options.find((item) => item?.kind === "allow_once" || item?.kind === "allow_always");
  if (permissionIsReadOnly(request) && allow?.optionId) {
    return { outcome: { outcome: "selected", optionId: allow.optionId } };
  }
  const reject = options.find((item) => item?.kind === "reject_once" || item?.kind === "reject_always");
  if (reject?.optionId) {
    return { outcome: { outcome: "selected", optionId: reject.optionId } };
  }
  return { outcome: { outcome: "cancelled" } };
}

function withTimeout(promise, timeoutMs, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function waitForChildExit(child, timeoutMs) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    child.once("exit", () => finish(true));
    child.once("error", () => finish(true));
  });
}

async function stopChild(child) {
  if (!child) return true;
  try {
    child.stdin.end();
  } catch {
    // The child may have exited before cleanup began.
  }
  let exited = await waitForChildExit(child, CHILD_EXIT_TIMEOUT_MS);
  if (!exited) {
    try {
      child.kill("SIGTERM");
    } catch {
      // The process may have exited between the bounded wait and kill.
    }
    exited = await waitForChildExit(child, CHILD_EXIT_TIMEOUT_MS);
  }
  if (!exited) {
    try {
      child.kill("SIGKILL");
    } catch {
      // Preserve the failed cleanup result below.
    }
    exited = await waitForChildExit(child, CHILD_EXIT_TIMEOUT_MS);
  }
  return exited;
}

async function runTask(options, prompt, signalState) {
  const sdk = await loadSdk(options.sdkEntry, options.harness);
  const child = spawn(options.agentArgv[0], options.agentArgv.slice(1), {
    cwd: options.cwd,
    env: { ...process.env },
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stdin.on("error", () => {});
  child.stdout.on("error", () => {});
  child.stderr.on("error", () => {});
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));

  const stream = sdk.ndJsonStream(
    Writable.toWeb(child.stdin),
    Readable.toWeb(child.stdout),
  );
  let sessionId;
  let connection;
  const messageText = new Map();
  let lastMessageId;
  let promptInFlight = false;
  let cleanupStarted = false;

  const app = sdk
    .client({ name: CLIENT_NAME })
    .onRequest(sdk.methods.client.session.requestPermission, ({ params }) => selectPermission(params))
    .onNotification(sdk.methods.client.session.update, ({ params }) => {
      if (params?.sessionId !== sessionId) return;
      const text = textFromUpdate(params.update);
      const messageId = params.update?.messageId;
      if (!text || typeof messageId !== "string" || messageId.length === 0) return;
      lastMessageId = messageId;
      messageText.set(messageId, `${messageText.get(messageId) ?? ""}${text}`);
    });
  for (const method of [
    sdk.methods.client.fs.readTextFile,
    sdk.methods.client.fs.writeTextFile,
    sdk.methods.client.terminal.create,
    sdk.methods.client.terminal.output,
    sdk.methods.client.terminal.release,
    sdk.methods.client.terminal.waitForExit,
    sdk.methods.client.terminal.kill,
  ]) {
    app.onRequest(method, () => {
      throw new Error("scoped ACP filesystem and terminal bridges are disabled");
    });
  }
  connection = app.connect(stream);
  const agent = connection.agent;
  const request = (method, params, requestOptions = undefined) => agent.request(method, params, requestOptions);

  const cancelActive = async () => {
    if (!connection || !sessionId || cleanupStarted) return;
    try {
      await withTimeout(
        agent.notify(sdk.methods.agent.session.cancel, { sessionId }),
        CLEANUP_TIMEOUT_MS,
        "session cancellation",
      );
    } catch (error) {
      process.stderr.write(`scoped ACP cancellation failed: ${error?.message ?? error}\n`);
    }
  };
  signalState.cancelActive = cancelActive;

  let primaryError;
  let sessionClosed = false;
  let childExited = false;
  try {
    if (signalState.interrupted) fail(signalState.reason);
    await request(sdk.methods.agent.initialize, {
      protocolVersion: 1,
      clientCapabilities: {
        fs: { readTextFile: false, writeTextFile: false },
        terminal: false,
      },
      clientInfo: { name: CLIENT_NAME, version: CLIENT_VERSION },
    });
    if (signalState.interrupted) fail(signalState.reason);
    const created = await request(sdk.methods.agent.session.new, buildSessionRequest(options));
    if (!created || typeof created.sessionId !== "string" || created.sessionId.length === 0) {
      fail("session/new returned no session id");
    }
    sessionId = created.sessionId;
    signalState.sessionId = sessionId;
    if (signalState.interrupted) {
      await cancelActive();
      fail(signalState.reason);
    }

    const modelResponse = await request(sdk.methods.agent.session.setConfigOption, {
      sessionId,
      configId: "model",
      value: options.model,
    });
    const selectedModel = requireConfigValue(modelResponse, "model", "model selection");
    if (selectedModel !== options.model) fail("model selection does not match requested model");
    const effortId = options.harness === "codex" ? "reasoning_effort" : "effort";
    const effortResponse = await request(sdk.methods.agent.session.setConfigOption, {
      sessionId,
      configId: effortId,
      value: options.effort,
    });
    const selectedEffort = requireConfigValue(effortResponse, effortId, "effort selection");
    if (selectedEffort !== options.effort) fail("effort selection does not match requested effort");
    if (requireConfigValue(effortResponse, "model", "effort selection") !== options.model) {
      fail("model selection does not match requested model after effort selection");
    }
    if (signalState.interrupted) {
      await cancelActive();
      fail(signalState.reason);
    }

    const promptAbort = new AbortController();
    signalState.promptAbort = promptAbort;
    const timeout = setTimeout(() => {
      signalState.timedOut = true;
      void cancelActive();
      promptAbort.abort(new Error("ACP prompt timed out"));
    }, options.timeoutMs);
    let promptResponse;
    try {
      promptInFlight = true;
      promptResponse = await request(
        sdk.methods.agent.session.prompt,
        { sessionId, prompt: [{ type: "text", text: prompt }] },
        { cancellationSignal: promptAbort.signal },
      );
    } catch (error) {
      if (signalState.interrupted) fail(signalState.reason);
      if (signalState.timedOut) fail("ACP prompt timed out");
      throw error;
    } finally {
      promptInFlight = false;
      clearTimeout(timeout);
    }
    if (signalState.interrupted) fail(signalState.reason);
    if (signalState.timedOut) fail("ACP prompt timed out");
    if (!promptResponse || promptResponse.stopReason !== "end_turn") {
      fail(`ACP prompt stopped with ${String(promptResponse?.stopReason ?? "unknown")}`);
    }
    const output = lastMessageId === undefined ? "" : messageText.get(lastMessageId) ?? "";
    if (output.length === 0) fail("ACP prompt completed without a tagged final agent message");

    signalState.result = {
      output,
      session_id: sessionId,
      model: selectedModel,
      effort: selectedEffort,
      cleanup_confirmed: true,
    };
  } catch (error) {
    primaryError = error;
  } finally {
    if (signalState.interrupted && promptInFlight) await cancelActive();
    cleanupStarted = true;
    if (connection && sessionId) {
      try {
        await withTimeout(
          request(sdk.methods.agent.session.close, { sessionId }),
          CLEANUP_TIMEOUT_MS,
          "session close",
        );
        sessionClosed = true;
      } catch (error) {
        process.stderr.write(`scoped ACP session close failed: ${error?.message ?? error}\n`);
      }
    }
    try {
      connection?.close();
    } catch (error) {
      process.stderr.write(`scoped ACP connection close failed: ${error?.message ?? error}\n`);
    }
    childExited = await stopChild(child);
  }
  if (primaryError) {
    if (!sessionClosed || !childExited) {
      primaryError = new Error(`${primaryError.message}; cleanup unconfirmed`);
    }
    throw primaryError;
  }
  if (!sessionClosed || !childExited) fail("cleanup could not be confirmed");
  return signalState.result;
}

function readPrompt(signal) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    const onAbort = () => {
      cleanup();
      reject(signal.reason instanceof Error ? signal.reason : new Error("prompt input cancelled"));
    };
    const onData = (chunk) => chunks.push(Buffer.from(chunk));
    const onEnd = () => {
      cleanup();
      const value = Buffer.concat(chunks).toString("utf8");
      if (value.length === 0) reject(new Error("prompt stdin must not be empty"));
      else resolve(value);
    };
    const cleanup = () => {
      process.stdin.off("data", onData);
      process.stdin.off("end", onEnd);
      signal.removeEventListener("abort", onAbort);
    };
    process.stdin.on("data", onData);
    process.stdin.once("end", onEnd);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function main() {
  const signalState = {
    interrupted: false,
    reason: "",
    timedOut: false,
    sessionId: undefined,
    promptAbort: undefined,
    cancelActive: undefined,
    result: undefined,
  };
  const inputAbort = new AbortController();
  const onSignal = (signal) => {
    if (signalState.interrupted) return;
    signalState.interrupted = true;
    signalState.reason = `received ${signal}`;
    inputAbort.abort(new Error(signalState.reason));
    signalState.promptAbort?.abort(new Error(signalState.reason));
    void signalState.cancelActive?.();
  };
  const onSigterm = () => onSignal("SIGTERM");
  const onSigint = () => onSignal("SIGINT");
  process.once("SIGTERM", onSigterm);
  process.once("SIGINT", onSigint);
  try {
    const options = parseCliArgs(process.argv.slice(2));
    const prompt = await readPrompt(inputAbort.signal);
    if (signalState.interrupted) fail(signalState.reason);
    const result = await runTask(options, prompt, signalState);
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } finally {
    process.off("SIGTERM", onSigterm);
    process.off("SIGINT", onSigint);
  }
}

let invokedAsScript = false;
const runningEval = process.execArgv.some((value) => value === "-e" || value === "--eval");
if (!runningEval && process.argv[1] && !process.argv[1].startsWith("-")) {
  try {
    invokedAsScript = fs.realpathSync.native(process.argv[1]) === fileURLToPath(import.meta.url);
  } catch {
    invokedAsScript = false;
  }
}
if (invokedAsScript) {
  main().catch((error) => {
    process.stderr.write(`${error?.message ?? error}\n`);
    process.exitCode = 1;
  });
}
