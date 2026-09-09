#!/usr/bin/env node

import { TextDecoder } from "node:util";

import { configSnapshot } from "./codex_scoped_bridge.mjs";

const MAX_TOTAL_BYTES = 16 * 1024 * 1024;
const MAX_METHOD_BYTES = 256;
const MAX_ID_BYTES = 256;
const MAX_NOTIFICATIONS = 64;
const REQUEST_TIMEOUT_MS = 15_000;
const WRITE_TIMEOUT_MS = 15_000;
const CHILD_GRACE_MS = 1_000;
const CHILD_TERM_GRACE_MS = 1_000;
const CHILD_KILL_GRACE_MS = 1_000;
const CLIENT_INFO = Object.freeze({
  name: "@agentclientprotocol/codex-acp",
  title: "Codex ACP",
  version: "1.10.0",
});
const CLEANUP_COMPLETE = Symbol("cleanup-complete");

class InspectFailure extends Error {
  constructor(reason) {
    super(`codex scoped inspect failed: ${reason}`);
    this.name = "InspectFailure";
  }
}

function fail(reason) {
  throw new InspectFailure(reason);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function validText(value) {
  return typeof value === "string" && value.length > 0 && !value.includes("\0") &&
    [...value].every((character) => {
      const code = character.codePointAt(0);
      return code === undefined || code >= 0x20;
    });
}

function validId(value) {
  return (typeof value === "string" && value.length > 0 && value.length <= MAX_ID_BYTES && !value.includes("\0")) ||
    (typeof value === "number" && Number.isSafeInteger(value));
}

function idKey(value) {
  return typeof value === "string" ? `s:${value}` : `n:${String(value)}`;
}

function validateOptions(options) {
  if (!isObject(options)) fail("invalid options");
  const allowed = new Set(["child", "workspace", "model", "effort"]);
  if (Object.keys(options).some((key) => !allowed.has(key))) fail("invalid options");
  for (const key of ["child", "workspace", "model", "effort"]) {
    if (!hasOwn(options, key)) fail("invalid options");
  }
  if (!validText(options.workspace) || !validText(options.model) || !validText(options.effort)) {
    fail("invalid options");
  }
  return options;
}

function validateChild(child) {
  if (!child || typeof child.on !== "function" || typeof child.once !== "function" ||
      typeof child.kill !== "function" || !child.stdin || typeof child.stdin.write !== "function" ||
      typeof child.stdin.end !== "function" || !child.stdout || typeof child.stdout.on !== "function" ||
      typeof child.stdout.once !== "function") {
    fail("invalid child process");
  }
  if (child.stderr !== null && child.stderr !== undefined && typeof child.stderr.on !== "function") {
    fail("invalid child process");
  }
}

function currentExitState(child) {
  const code = child?.exitCode ?? null;
  const signal = child?.signalCode ?? null;
  return {
    exited: code !== null || signal !== null,
    code,
    signal,
    error: false,
  };
}

function waitForExit(child, state, timeoutMs) {
  if (state.exited) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.off?.("exit", finish);
      child.off?.("close", finish);
      resolve(true);
    };
    timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.off?.("exit", finish);
      child.off?.("close", finish);
      resolve(false);
    }, timeoutMs);
    child.once("exit", finish);
    child.once("close", finish);
  });
}

async function stopChild(child, state) {
  try {
    if (child?.stdin && !child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.end();
  } catch {
    // Cleanup remains bounded and the caller reports the original phase failure.
  }
  let stopped = await waitForExit(child, state, CHILD_GRACE_MS);
  if (!stopped) {
    try {
      if (!state.exited) child.kill("SIGTERM");
    } catch {
      // The process may have exited between the state check and kill.
    }
    stopped = await waitForExit(child, state, CHILD_TERM_GRACE_MS);
  }
  if (!stopped) {
    try {
      if (!state.exited) child.kill("SIGKILL");
    } catch {
      // Preserve the bounded cleanup result.
    }
    stopped = await waitForExit(child, state, CHILD_KILL_GRACE_MS);
  }
  return stopped;
}

async function bestEffortStop(child) {
  if (!child || typeof child.once !== "function" || typeof child.kill !== "function") return false;
  const state = currentExitState(child);
  try {
    return await stopChild(child, state);
  } catch {
    return false;
  }
}

function encodeFrame(message) {
  let text;
  try {
    text = JSON.stringify(message);
  } catch {
    fail("request encoding failed");
  }
  if (typeof text !== "string" || text.includes('"jsonrpc"')) fail("request encoding failed");
  const bytes = Buffer.byteLength(text, "utf8") + 1;
  if (bytes > MAX_TOTAL_BYTES) fail("request exceeds size limit");
  return `${text}\n`;
}

function writeFrame(stream, message) {
  const text = encodeFrame(message);
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      stream.off?.("error", onError);
      stream.off?.("drain", onDrain);
      if (error) reject(error);
      else resolve();
    };
    const onError = () => finish(new InspectFailure("child input failed"));
    const onDrain = () => finish();
    timer = setTimeout(() => finish(new InspectFailure("child input timed out")), WRITE_TIMEOUT_MS);
    stream.once?.("error", onError);
    try {
      const accepted = stream.write(text, "utf8", (error) => finish(error ? new InspectFailure("child input failed") : undefined));
      if (!accepted && !settled) stream.once?.("drain", onDrain);
    } catch {
      finish(new InspectFailure("child input failed"));
    }
  });
}

function validateChildFrame(value) {
  if (!isObject(value) || hasOwn(value, "jsonrpc")) fail("wire marker is not allowed");
  const hasMethod = hasOwn(value, "method");
  const hasId = hasOwn(value, "id");
  const hasResult = hasOwn(value, "result");
  const hasError = hasOwn(value, "error");
  if (hasMethod) {
    if (hasId) fail("unknown server request");
    if (!validText(value.method) || Buffer.byteLength(value.method, "utf8") > MAX_METHOD_BYTES) {
      fail("malformed child notification");
    }
    if (Object.keys(value).some((key) => !["method", "params"].includes(key))) {
      fail("malformed child notification");
    }
    return { kind: "notification", method: value.method };
  }
  if (!hasId || !validId(value.id) || (hasResult === hasError) ||
      Object.keys(value).some((key) => !["id", "result", "error"].includes(key))) {
    fail("malformed child response");
  }
  if (hasError) fail("child RPC failed");
  return { kind: "response", id: value.id, result: value.result };
}

class ChildReader {
  constructor(stream, onFrame, onFailure, onEnd) {
    this.stream = stream;
    this.onFrame = onFrame;
    this.onFailure = onFailure;
    this.onEnd = onEnd;
    this.buffer = Buffer.alloc(0);
    this.totalBytes = 0;
    this.notifications = 0;
    this.stopped = false;
    this.ended = false;
    this.decoder = new TextDecoder("utf-8", { fatal: true });
    this.handleData = (chunk) => this.data(chunk);
    this.handleEnd = () => this.end();
    this.handleClose = () => this.end();
    this.handleError = () => this.failure("child output failed");
    stream.on("data", this.handleData);
    stream.once("end", this.handleEnd);
    stream.once("close", this.handleClose);
    stream.once("error", this.handleError);
  }

  failure(reason) {
    if (this.stopped) return;
    this.stopped = true;
    this.onFailure(reason);
  }

  data(chunk) {
    if (this.stopped) return;
    let bytes;
    try {
      bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    } catch {
      this.failure("child output is invalid");
      return;
    }
    if (this.totalBytes + bytes.length > MAX_TOTAL_BYTES) {
      this.failure("child output exceeds size limit");
      return;
    }
    this.totalBytes += bytes.length;
    let offset = 0;
    while (!this.stopped && offset < bytes.length) {
      const newline = bytes.indexOf(0x0a, offset);
      if (newline < 0) {
        const tail = bytes.subarray(offset);
        if (this.buffer.length + tail.length > MAX_TOTAL_BYTES) {
          this.failure("child output exceeds size limit");
          return;
        }
        this.buffer = Buffer.concat([this.buffer, tail]);
        return;
      }
      const line = bytes.subarray(offset, newline);
      const frame = this.buffer.length === 0 ? line : Buffer.concat([this.buffer, line]);
      this.buffer = Buffer.alloc(0);
      offset = newline + 1;
      if (frame.length === 0 || frame.length + 1 > MAX_TOTAL_BYTES) {
        this.failure("malformed child frame");
        return;
      }
      let message;
      try {
        message = JSON.parse(this.decoder.decode(frame[frame.length - 1] === 0x0d ? frame.subarray(0, -1) : frame));
      } catch {
        this.failure("malformed child frame");
        return;
      }
      try {
        const parsed = validateChildFrame(message);
        if (parsed.kind === "notification") {
          this.notifications += 1;
          if (this.notifications > MAX_NOTIFICATIONS) fail("too many child notifications");
          continue;
        }
        this.onFrame(parsed);
      } catch (error) {
        this.failure(error instanceof InspectFailure ? error.message.replace(/^codex scoped inspect failed: /u, "") : "malformed child frame");
        return;
      }
    }
  }

  end() {
    if (this.stopped || this.ended) return;
    this.ended = true;
    if (this.buffer.length !== 0) {
      this.failure("child output ended mid-frame");
      return;
    }
    this.onEnd();
  }

  dispose() {
    if (this.stopped) {
      // Keep removing the listeners below even after a failure.
    }
    this.stopped = true;
    this.stream.off?.("data", this.handleData);
    this.stream.off?.("end", this.handleEnd);
    this.stream.off?.("close", this.handleClose);
    this.stream.off?.("error", this.handleError);
    this.stream.pause?.();
  }
}

function validateConfig(response, model, effort) {
  let snapshot;
  try {
    snapshot = configSnapshot(response);
  } catch {
    fail("config response is invalid");
  }
  if (!/^[0-9a-f]{64}$/u.test(snapshot)) fail("config response is invalid");
  const config = response.config;
  if (config.model_provider !== undefined && config.model_provider !== null && config.model_provider !== "openai") {
    fail("config provider does not match selected provider");
  }
  if (config.model !== undefined && config.model !== null && config.model !== model) {
    fail("config model does not match selected model");
  }
  if (config.model_reasoning_effort !== undefined && config.model_reasoning_effort !== null && config.model_reasoning_effort !== effort) {
    fail("config effort does not match selected effort");
  }
  return snapshot;
}

async function inspectWithChild({ child, workspace, model, effort }) {
  validateChild(child);
  const state = currentExitState(child);
  if (state.exited) fail("child was already stopped");
  let failure = null;
  let cleanupStarted = false;
  let phaseComplete = false;
  let reader;
  let childInputFailure;
  const pending = new Map();
  let nextId = 0;
  let lastResponseMethod = null;
  const rejectPending = (reason) => {
    for (const entry of pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(reason);
    }
    pending.clear();
  };
  const onChildError = () => {
    state.error = true;
    if (!cleanupStarted && failure === null) failure = new InspectFailure("child process failed");
    rejectPending(failure ?? new InspectFailure("child process failed"));
  };
  const onChildExit = (code, signal) => {
    state.exited = true;
    state.code = code ?? child.exitCode ?? null;
    state.signal = signal ?? child.signalCode ?? null;
    if (!cleanupStarted && !phaseComplete && lastResponseMethod !== "config/read" && failure === null) {
      failure = new InspectFailure("child exited before inspection completed");
    }
    rejectPending(failure ?? new InspectFailure("child exited before inspection completed"));
  };
  const onChildClose = (code, signal) => onChildExit(code, signal);
  const onInputError = () => {
    childInputFailure = new InspectFailure("child input failed");
    if (failure === null) failure = childInputFailure;
    rejectPending(failure);
  };
  const onSignal = (signal) => {
    if (failure === null && !cleanupStarted) failure = new InspectFailure(`received ${signal}`);
    rejectPending(failure ?? new InspectFailure(`received ${signal}`));
  };
  const signalHandlers = {
    SIGTERM: () => onSignal("SIGTERM"),
    SIGINT: () => onSignal("SIGINT"),
  };
  const onStderrData = () => {};
  const onStderrError = () => {
    if (!cleanupStarted) rejectFailure("child stderr failed");
  };
  const rejectFailure = (reason) => {
    if (failure === null) failure = new InspectFailure(reason);
    rejectPending(failure);
  };
  const onFrame = (frame) => {
    if (frame.kind !== "response") return;
    const key = idKey(frame.id);
    const entry = pending.get(key);
    if (!entry) {
      rejectFailure("unexpected child response");
      return;
    }
    pending.delete(key);
    clearTimeout(entry.timer);
    lastResponseMethod = entry.method;
    entry.resolve(frame.result);
  };
  const request = async (method, params, phase) => {
    if (failure !== null) throw failure;
    const id = `codex-scoped-inspect-${++nextId}`;
    const response = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        rejectFailure(`timed out waiting for ${phase}`);
      }, REQUEST_TIMEOUT_MS);
      pending.set(idKey(id), { resolve, reject, timer, method });
    });
    try {
      await writeFrame(child.stdin, {
        id,
        method,
        ...(params === undefined ? {} : { params }),
      });
    } catch {
      const entry = pending.get(idKey(id));
      if (entry) {
        pending.delete(idKey(id));
        clearTimeout(entry.timer);
      }
      rejectFailure(`${phase} request failed`);
    }
    return await response;
  };
  const notify = async (method) => {
    if (failure !== null) throw failure;
    try {
      await writeFrame(child.stdin, { method });
    } catch {
      rejectFailure("initialized notification failed");
      throw failure;
    }
  };
  const cleanupListeners = () => {
    reader?.dispose();
    child.off?.("error", onChildError);
    child.off?.("exit", onChildExit);
    child.off?.("close", onChildClose);
    child.stdin.off?.("error", onInputError);
    child.stderr?.off?.("data", onStderrData);
    child.stderr?.off?.("error", onStderrError);
    process.off("SIGTERM", signalHandlers.SIGTERM);
    process.off("SIGINT", signalHandlers.SIGINT);
  };
  try {
    child.on("error", onChildError);
    child.once("exit", onChildExit);
    child.once("close", onChildClose);
    child.stdin.on?.("error", onInputError);
    child.stderr?.on?.("data", onStderrData);
    child.stderr?.on?.("error", onStderrError);
    process.once("SIGTERM", signalHandlers.SIGTERM);
    process.once("SIGINT", signalHandlers.SIGINT);
    reader = new ChildReader(
      child.stdout,
      onFrame,
      (reason) => rejectFailure(reason),
      () => {
        if (!cleanupStarted && !phaseComplete) rejectFailure("child output EOF");
      },
    );

    const initialized = await request(
      "initialize",
      {
        capabilities: { experimentalApi: true, requestAttestation: false },
        clientInfo: { ...CLIENT_INFO },
      },
      "initialize",
    );
    if (!isObject(initialized)) fail("initialize response is invalid");
    await notify("initialized");
    const requirements = await request("configRequirements/read", undefined, "config requirements");
    if (!isObject(requirements) || !hasOwn(requirements, "requirements") || requirements.requirements !== null) {
      fail("config requirements are not empty");
    }
    const response = await request(
      "config/read",
      { cwd: workspace, includeLayers: true },
      "config read",
    );
    const snapshot = validateConfig(response, model, effort);
    phaseComplete = true;
    return { configSnapshot: snapshot };
  } catch (error) {
    if (error instanceof InspectFailure) {
      if (failure === null) failure = error;
    } else if (failure === null) {
      failure = new InspectFailure("inspection failed");
    }
    throw failure;
  } finally {
    cleanupStarted = true;
    let stopped = false;
    try {
      stopped = await stopChild(child, state);
    } catch {
      if (failure === null) failure = new InspectFailure("child cleanup failed");
    }
    try {
      cleanupListeners();
    } catch {
      if (failure === null) failure = new InspectFailure("child cleanup failed");
    }
    if (failure === null && (!stopped || state.error || !state.exited || state.code !== 0 || state.signal !== null)) {
      failure = new InspectFailure("child cleanup was not verified");
    }
    if (failure !== null) failure[CLEANUP_COMPLETE] = true;
    if (failure !== null && phaseComplete) throw failure;
  }
}

export async function inspectConfig(options) {
  let candidate;
  try {
    candidate = isObject(options) ? options.child : undefined;
    validateOptions(options);
    return await inspectWithChild(options);
  } catch (error) {
    if (candidate && !(error instanceof InspectFailure && error[CLEANUP_COMPLETE])) {
      await bestEffortStop(candidate);
    }
    if (!(error instanceof InspectFailure)) {
      throw new InspectFailure("inspection failed");
    }
    throw error;
  }
}
