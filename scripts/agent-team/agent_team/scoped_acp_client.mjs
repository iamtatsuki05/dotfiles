#!/usr/bin/env node

import fs from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import { fileURLToPath, pathToFileURL } from "node:url";

const READ_TOOLS = Object.freeze(["Read", "Grep", "Glob"]);
const WRITE_TOOLS = Object.freeze(["Write", "Edit"]);
const ASK_USER_TOOL = "AskUserQuestion";
const WORKSPACE_TOOLS = Object.freeze([...READ_TOOLS, ...WRITE_TOOLS]);
const READ_TOOL_NAMES = new Set(READ_TOOLS.map((name) => name.toLowerCase()));
const CLIENT_NAME = "agent-team-scoped-acp-client";
const CLIENT_VERSION = "1";
const CLEANUP_TIMEOUT_MS = 2_000;
const CHILD_EXIT_TIMEOUT_MS = 2_000;
const MAX_QUESTION_TOOL_CALL_ID_CHARS = 256;
const MAX_RESULT_BYTES = 1 * 1024 * 1024;
const RESULT_FILE_NAME = "client-result.json";
const RESULT_TEMP_NAME = "client-result.pending";
const LAUNCH_NONCE_RE = /^[a-z0-9]{8,64}$/;
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

function currentUid() {
  const uid = process.getuid?.();
  if (!Number.isSafeInteger(uid)) fail("current uid is unavailable");
  return uid;
}

function validateLaunchNonce(value) {
  const nonce = assertString(value, "launch-nonce");
  if (!LAUNCH_NONCE_RE.test(nonce)) fail("launch-nonce is invalid");
  return nonce;
}

function readPrivateDirectory(directory, label) {
  let info;
  try {
    info = fs.lstatSync(directory);
  } catch (error) {
    fail(`${label} is unavailable: ${error?.message ?? error}`);
  }
  if (info.isSymbolicLink() || !info.isDirectory()) fail(`${label} must be a real directory`);
  const canonical = fs.realpathSync.native(directory);
  let canonicalInfo;
  try {
    canonicalInfo = fs.lstatSync(canonical);
  } catch (error) {
    fail(`${label} is unavailable: ${error?.message ?? error}`);
  }
  const uid = currentUid();
  if (
    canonicalInfo.isSymbolicLink() ||
    !canonicalInfo.isDirectory() ||
    canonicalInfo.uid !== uid ||
    (canonicalInfo.mode & 0o777) !== 0o700
  ) {
    fail(`${label} must be current-user-owned mode 0700`);
  }
  return {
    path: canonical,
    identity: {
      dev: canonicalInfo.dev,
      ino: canonicalInfo.ino,
      uid: canonicalInfo.uid,
      mode: canonicalInfo.mode & 0o777,
    },
  };
}

function assertAbsent(pathname, label) {
  try {
    fs.lstatSync(pathname);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    fail(`${label} is unavailable: ${error?.message ?? error}`);
  }
  fail(`${label} already exists`);
}

export function validateResultFileSpec(rawPath) {
  const value = assertString(rawPath, "result-file");
  if (!path.isAbsolute(value)) fail("result-file must be absolute");
  const absolute = path.resolve(value);
  if (path.basename(absolute) !== RESULT_FILE_NAME) {
    fail(`result-file basename must be ${RESULT_FILE_NAME}`);
  }
  const parent = readPrivateDirectory(path.dirname(absolute), "result-file parent");
  const resultPath = path.join(parent.path, RESULT_FILE_NAME);
  const tempPath = path.join(parent.path, RESULT_TEMP_NAME);
  assertAbsent(resultPath, "result-file");
  assertAbsent(tempPath, "result-file temporary path");
  return {
    path: resultPath,
    tempPath,
    parent: parent.path,
    parentIdentity: parent.identity,
  };
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
    "--question-socket",
    "--result-file",
    "--launch-nonce",
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
  const harness = parseHarness(values["--harness"]);
  let questionSocket;
  if (values["--question-socket"] !== undefined) {
    if (harness !== "claude") fail("--question-socket is only available for claude");
    const rawSocket = assertString(values["--question-socket"], "question socket");
    if (!path.isAbsolute(rawSocket)) fail("question socket must be absolute");
    questionSocket = path.resolve(rawSocket);
  }
  const hasResultFile = values["--result-file"] !== undefined;
  const hasLaunchNonce = values["--launch-nonce"] !== undefined;
  if (hasResultFile !== hasLaunchNonce) {
    fail("result-file and launch-nonce must be provided together");
  }
  const resultFile = hasResultFile ? validateResultFileSpec(values["--result-file"]) : undefined;
  const launchNonce = hasLaunchNonce ? validateLaunchNonce(values["--launch-nonce"]) : undefined;
  return {
    harness,
    sdkEntry: absolutePath(values["--sdk-entry"], "SDK entry"),
    agentArgv,
    cwd: existingDirectory(values["--cwd"], "cwd"),
    permission: parsePermission(values["--permission"]),
    model: assertString(values["--model"], "model"),
    effort: assertString(values["--effort"], "effort"),
    instructions: assertString(values["--instructions"], "instructions"),
    timeoutMs: parsePositiveInteger(values["--timeout-ms"], "timeout-ms"),
    questionSocket,
    resultFile,
    launchNonce,
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

function fixedTools(permission, questionsEnabled = false) {
  const tools = permission === "workspace-write" ? [...WORKSPACE_TOOLS] : [...READ_TOOLS];
  if (questionsEnabled) tools.push(ASK_USER_TOOL);
  return tools;
}

export function buildSessionRequest(options) {
  if (parseHarness(options.harness) === "codex") {
    return { cwd: options.cwd, mcpServers: [] };
  }
  const questionsEnabled = options.questions === true || options.questionSocket !== undefined;
  const tools = fixedTools(options.permission, questionsEnabled);
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

function cleanupConfirmed(cleanup) {
  if (!cleanup.spawnAttempted) return true;
  if (!cleanup.childSpawned || !cleanup.childExited) return false;
  if (cleanup.connectionAttempted && !cleanup.connectionClosed) return false;
  if (cleanup.sessionNewAttempted) {
    return typeof cleanup.sessionId === "string" && cleanup.sessionClosed;
  }
  return true;
}

function boundedError(error) {
  const message = String(error?.message ?? error);
  return [...message].slice(0, 4_000).join("");
}

function failureReceipt(error, options, cleanup) {
  return {
    error: boundedError(error),
    session_id: cleanup.sessionId,
    model: options.model,
    effort: options.effort,
    cleanup_confirmed: cleanupConfirmed(cleanup),
  };
}

function assertResultParent(spec) {
  const parent = readPrivateDirectory(spec.parent, "result-file parent");
  const expected = spec.parentIdentity;
  if (
    parent.identity.dev !== expected.dev ||
    parent.identity.ino !== expected.ino ||
    parent.identity.uid !== expected.uid ||
    parent.identity.mode !== expected.mode
  ) {
    fail("result-file parent changed during execution");
  }
}

function assertOwnedResult(pathname, label) {
  let info;
  try {
    info = fs.lstatSync(pathname);
  } catch (error) {
    fail(`${label} is unavailable: ${error?.message ?? error}`);
  }
  const uid = currentUid();
  if (info.isSymbolicLink() || !info.isFile() || info.uid !== uid || (info.mode & 0o777) !== 0o600) {
    fail(`${label} must be current-user-owned mode 0600 regular file`);
  }
}

function publishResultReceipt(spec, launchNonce, receipt, cleanup) {
  if (cleanup.receiptAttempted) fail("result receipt publication was already attempted");
  cleanup.receiptAttempted = true;
  cleanup.receiptPublicationUnconfirmed = true;
  const envelope = { version: 1, launch_nonce: launchNonce, receipt };
  const encoded = Buffer.from(`${JSON.stringify(envelope)}\n`, "utf8");
  if (encoded.length > MAX_RESULT_BYTES) fail("result receipt exceeds 1 MiB");
  assertResultParent(spec);
  assertAbsent(spec.path, "result-file");
  assertAbsent(spec.tempPath, "result-file temporary path");
  let fd;
  try {
    fd = fs.openSync(
      spec.tempPath,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW,
      0o600,
    );
    fs.writeFileSync(fd, encoded);
    fs.fsyncSync(fd);
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
  assertResultParent(spec);
  assertOwnedResult(spec.tempPath, "result-file temporary path");
  fs.linkSync(spec.tempPath, spec.path);
  fs.unlinkSync(spec.tempPath);
  const directoryFd = fs.openSync(
    spec.parent,
    fs.constants.O_RDONLY | (fs.constants.O_DIRECTORY ?? 0),
  );
  try {
    fs.fsyncSync(directoryFd);
  } finally {
    fs.closeSync(directoryFd);
  }
  assertResultParent(spec);
  assertOwnedResult(spec.path, "result-file");
  cleanup.receiptPublicationUnconfirmed = false;
  cleanup.receiptPublished = true;
}

function writeStdout(value) {
  const encoded = `${JSON.stringify(value)}\n`;
  return new Promise((resolve, reject) => {
    let callbackDone = false;
    let drained = true;
    let settled = false;
    const finish = (error) => {
      if (settled || !callbackDone || !drained) return;
      settled = true;
      process.stdout.off("error", onError);
      if (error) reject(error);
      else resolve();
    };
    const onError = (error) => {
      settled = true;
      process.stdout.off("drain", onDrain);
      process.stdout.off("error", onError);
      reject(error);
    };
    const onDrain = () => {
      drained = true;
      process.stdout.off("drain", onDrain);
      finish();
    };
    process.stdout.once("error", onError);
    drained = process.stdout.write(encoded, (error) => {
      callbackDone = true;
      finish(error);
    });
    if (!drained) process.stdout.once("drain", onDrain);
  });
}

async function runTask(options, prompt, signalState) {
  const cleanup = signalState.cleanup;
  const sdk = await loadSdk(options.sdkEntry, options.harness);
  let questionClient;
  if (options.questionSocket !== undefined) {
    const questionModule = await import("./scoped_question_client.mjs");
    options.questionSocket = questionModule.validateQuestionSocket(options.questionSocket);
    if (options.questionSocket !== undefined) {
      questionClient = new questionModule.ScopedQuestionClient(options.questionSocket);
    }
  }
  cleanup.spawnAttempted = true;
  const child = spawn(options.agentArgv[0], options.agentArgv.slice(1), {
    cwd: options.cwd,
    env: { ...process.env },
    shell: false,
    stdio: ["pipe", "pipe", "pipe"],
  });
  cleanup.childSpawned = true;
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
  const observedAskToolCalls = new Set();
  const inFlightAskToolCalls = new Set();
  const consumedAskToolCalls = new Set();
  let observedAskToolCallCount = 0;
  let lastMessageId;
  let promptInFlight = false;
  let cleanupStarted = false;
  let promptAbort;
  let fatalQuestionError;
  let cancelActive;

  const recordFatalQuestionError = (error) => {
    if (fatalQuestionError || signalState.interrupted || signalState.timedOut) return;
    fatalQuestionError = error instanceof Error ? error : new Error(String(error));
    process.stderr.write(`scoped ACP question failed: ${fatalQuestionError.message}\n`);
    if (promptAbort && !promptAbort.signal.aborted) promptAbort.abort(fatalQuestionError);
    void cancelActive?.();
  };

  const app = sdk
    .client({ name: CLIENT_NAME })
    .onRequest(sdk.methods.client.session.requestPermission, ({ params }) => selectPermission(params))
    .onNotification(sdk.methods.client.session.update, ({ params }) => {
      if (params?.sessionId !== sessionId) return;
      const update = params.update;
      if (
        update?.sessionUpdate === "tool_call" &&
        update._meta?.claudeCode?.toolName === ASK_USER_TOOL
      ) {
        const toolCallId = update.toolCallId;
        if (
          typeof toolCallId !== "string" ||
          toolCallId.length === 0 ||
          [...toolCallId].length > MAX_QUESTION_TOOL_CALL_ID_CHARS
        ) {
          recordFatalQuestionError(new Error("AskUserQuestion tool call id is invalid"));
        } else if (
          observedAskToolCalls.has(toolCallId) ||
          inFlightAskToolCalls.has(toolCallId) ||
          consumedAskToolCalls.has(toolCallId)
        ) {
          recordFatalQuestionError(new Error("duplicate or empty AskUserQuestion tool call id"));
        } else if (observedAskToolCallCount >= 64) {
          recordFatalQuestionError(new Error("AskUserQuestion tool call limit exceeded"));
        } else {
          observedAskToolCalls.add(toolCallId);
          observedAskToolCallCount += 1;
        }
      }
      const text = textFromUpdate(update);
      const messageId = update?.messageId;
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
  if (questionClient) {
    app.onRequest(
      sdk.methods.client.elicitation.create,
      (params) => params,
      async ({ params, signal }) => {
        if (params?.mode !== "form") {
          recordFatalQuestionError(new Error("only form elicitations are supported"));
          return { action: "cancel" };
        }
        if (params?.sessionId !== sessionId) {
          recordFatalQuestionError(new Error("elicitation session does not match the active ACP session"));
          return { action: "cancel" };
        }
        if (typeof params?.toolCallId !== "string" || params.toolCallId.length === 0) {
          recordFatalQuestionError(new Error("elicitation tool call id is missing"));
          return { action: "cancel" };
        }
        if (inFlightAskToolCalls.has(params.toolCallId)) {
          recordFatalQuestionError(new Error("elicitation tool call already has an outstanding form"));
          return { action: "cancel" };
        }
        if (consumedAskToolCalls.has(params.toolCallId)) {
          recordFatalQuestionError(new Error("elicitation tool call was already consumed"));
          return { action: "cancel" };
        }
        if (!observedAskToolCalls.has(params.toolCallId)) {
          recordFatalQuestionError(new Error("elicitation tool call was not observed as AskUserQuestion"));
          return { action: "cancel" };
        }
        observedAskToolCalls.delete(params.toolCallId);
        inFlightAskToolCalls.add(params.toolCallId);
        try {
          const response = await questionClient.request(params, {
            signal,
            observedToolCall: true,
          });
          inFlightAskToolCalls.delete(params.toolCallId);
          consumedAskToolCalls.add(params.toolCallId);
          return response;
        } catch (error) {
          inFlightAskToolCalls.delete(params.toolCallId);
          recordFatalQuestionError(error);
          process.stderr.write(`scoped ACP question cancelled: ${error?.message ?? error}\n`);
          return { action: "cancel" };
        }
      },
    );
  }
  cleanup.connectionAttempted = true;
  try {
    connection = app.connect(stream);
  } catch (error) {
    cleanup.connectionClosed = false;
    cleanup.childExited = await stopChild(child);
    throw error;
  }
  const agent = connection.agent;
  const request = (method, params, requestOptions = undefined) => agent.request(method, params, requestOptions);

  cancelActive = async () => {
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
    const clientCapabilities = {
      fs: { readTextFile: false, writeTextFile: false },
      terminal: false,
      ...(questionClient ? { elicitation: { form: {} } } : {}),
    };
    await request(sdk.methods.agent.initialize, {
      protocolVersion: 1,
      clientCapabilities,
      clientInfo: { name: CLIENT_NAME, version: CLIENT_VERSION },
    });
    if (signalState.interrupted) fail(signalState.reason);
    cleanup.sessionNewAttempted = true;
    const created = await request(sdk.methods.agent.session.new, buildSessionRequest(options));
    if (!created || typeof created.sessionId !== "string" || created.sessionId.length === 0) {
      fail("session/new returned no session id");
    }
    sessionId = created.sessionId;
    cleanup.sessionId = sessionId;
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

    promptAbort = new AbortController();
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
      if (fatalQuestionError) throw fatalQuestionError;
      if (signalState.interrupted) fail(signalState.reason);
      if (signalState.timedOut) fail("ACP prompt timed out");
      throw error;
    } finally {
      promptInFlight = false;
      clearTimeout(timeout);
    }
    if (fatalQuestionError) throw fatalQuestionError;
    if (signalState.interrupted) fail(signalState.reason);
    if (signalState.timedOut) fail("ACP prompt timed out");
    if (!promptResponse || promptResponse.stopReason !== "end_turn") {
      fail(`ACP prompt stopped with ${String(promptResponse?.stopReason ?? "unknown")}`);
    }
    if (observedAskToolCalls.size > 0 || inFlightAskToolCalls.size > 0) {
      recordFatalQuestionError(new Error("AskUserQuestion did not complete its form elicitation"));
      throw fatalQuestionError;
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
    if ((signalState.interrupted || fatalQuestionError) && promptInFlight) await cancelActive();
    cleanupStarted = true;
    await questionClient?.close();
    if (connection && sessionId) {
      try {
        cleanup.sessionCloseAttempted = true;
        await withTimeout(
          request(sdk.methods.agent.session.close, { sessionId }),
          CLEANUP_TIMEOUT_MS,
          "session close",
        );
        sessionClosed = true;
        cleanup.sessionClosed = true;
      } catch (error) {
        process.stderr.write(`scoped ACP session close failed: ${error?.message ?? error}\n`);
      }
    }
    try {
      connection?.close();
      cleanup.connectionClosed = true;
    } catch (error) {
      process.stderr.write(`scoped ACP connection close failed: ${error?.message ?? error}\n`);
    }
    childExited = await stopChild(child);
    cleanup.childExited = childExited;
  }
  if (primaryError) {
    if (!cleanupConfirmed(cleanup)) {
      primaryError = new Error(`${primaryError.message}; cleanup unconfirmed`);
    }
    throw primaryError;
  }
  if (!cleanupConfirmed(cleanup)) fail("cleanup could not be confirmed");
  signalState.result.cleanup_confirmed = true;
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
    stdoutAttempted: false,
    cleanup: {
      spawnAttempted: false,
      childSpawned: false,
      childExited: false,
      connectionAttempted: false,
      connectionClosed: false,
      sessionNewAttempted: false,
      sessionId: null,
      sessionCloseAttempted: false,
      sessionClosed: false,
      receiptAttempted: false,
      receiptPublicationUnconfirmed: false,
      receiptPublished: false,
    },
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
  let options;
  try {
    options = parseCliArgs(process.argv.slice(2));
    const prompt = await readPrompt(inputAbort.signal);
    if (signalState.interrupted) fail(signalState.reason);
    const result = await runTask(options, prompt, signalState);
    if (options.resultFile) {
      publishResultReceipt(options.resultFile, options.launchNonce, result, signalState.cleanup);
    }
    signalState.stdoutAttempted = true;
    await writeStdout(result);
  } catch (error) {
    let exitCode = 1;
    if (options?.resultFile) {
      const failure = failureReceipt(error, options, signalState.cleanup);
      if (!signalState.cleanup.receiptAttempted) {
        try {
          publishResultReceipt(options.resultFile, options.launchNonce, failure, signalState.cleanup);
        } catch (publishError) {
          failure.cleanup_confirmed = false;
          exitCode = 2;
          process.stderr.write(`scoped ACP result receipt failed: ${publishError?.message ?? publishError}\n`);
        }
      } else if (!signalState.cleanup.receiptPublished) {
        failure.cleanup_confirmed = false;
      }
      if (signalState.cleanup.receiptPublicationUnconfirmed) exitCode = 2;
      if (!signalState.stdoutAttempted) {
        signalState.stdoutAttempted = true;
        try {
          await writeStdout(failure);
        } catch (stdoutError) {
          process.stderr.write(`scoped ACP failure output failed: ${stdoutError?.message ?? stdoutError}\n`);
        }
      }
    }
    process.stderr.write(`${error?.message ?? error}\n`);
    process.exitCode = exitCode;
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
    if (process.exitCode === undefined || process.exitCode === 0) process.exitCode = 1;
  });
}
