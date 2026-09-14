#!/usr/bin/env node

import { TextDecoder } from "node:util";

import { CodexScopeBridge } from "./codex_scoped_bridge.mjs";

const MAX_FRAME_BYTES = 16 * 1024 * 1024;
const MAX_QUEUED_BYTES = 32 * 1024 * 1024;
const MAX_PENDING = 64;
const MAX_SEEN_IDS = 1_024;
const MAX_ID_LENGTH = 256;
const MAX_METHOD_LENGTH = 256;
const DRAIN_TIMEOUT_MS = 1_000;
const CHILD_GRACE_MS = 1_000;
const CHILD_TERM_GRACE_MS = 1_000;
const CHILD_KILL_GRACE_MS = 1_000;
const CHILD_ID_PREFIX = "codex-scoped-child-";

class TransportError extends Error {
  constructor(message, code = -32000) {
    super(message);
    this.name = "TransportError";
    this.code = code;
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isJsonValue(value, ancestors = new Set()) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object" || ancestors.has(value)) return false;
  const next = new Set(ancestors);
  next.add(value);
  if (Array.isArray(value)) return value.every((item) => isJsonValue(item, next));
  return Object.entries(value).every(([key, item]) => typeof key === "string" && isJsonValue(item, next));
}

function isValidId(value) {
  if (typeof value === "string") return value.length > 0 && value.length <= MAX_ID_LENGTH && !value.includes("\0");
  return typeof value === "number" && Number.isSafeInteger(value);
}

function idKey(value) {
  return typeof value === "string" ? `s:${value}` : `n:${String(value)}`;
}

function methodName(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_METHOD_LENGTH || value.includes("\0")) {
    throw new TransportError("invalid JSON-RPC method", -32600);
  }
  return value;
}

function validateRpcMessage(message, label) {
  if (!isObject(message) || hasOwn(message, "jsonrpc")) {
    throw new TransportError(`invalid ${label} JSON-RPC message`, -32600);
  }
  const hasId = hasOwn(message, "id");
  const hasMethod = hasOwn(message, "method");
  const hasResult = hasOwn(message, "result");
  const hasError = hasOwn(message, "error");
  const keys = Object.keys(message);
  if (hasMethod) {
    if (hasId) {
      if (!isValidId(message.id)) throw new TransportError(`invalid ${label} request id`, -32600);
      if (keys.some((key) => !["id", "method", "params"].includes(key))) {
        throw new TransportError(`invalid ${label} request fields`, -32600);
      }
      if (hasOwn(message, "params") && !isJsonValue(message.params)) {
        throw new TransportError(`invalid ${label} request params`, -32600);
      }
      return { kind: "request", id: message.id, method: methodName(message.method), params: message.params };
    }
    if (keys.some((key) => !["method", "params"].includes(key))) {
      throw new TransportError(`invalid ${label} notification fields`, -32600);
    }
    if (hasOwn(message, "params") && !isJsonValue(message.params)) {
      throw new TransportError(`invalid ${label} notification params`, -32600);
    }
    return { kind: "notification", method: methodName(message.method), params: message.params };
  }
  if (!hasId || !isValidId(message.id) || (!hasResult && !hasError) || (hasResult && hasError)) {
    throw new TransportError(`invalid ${label} response`, -32600);
  }
  if (keys.some((key) => !["id", "result", "error"].includes(key))) {
    throw new TransportError(`invalid ${label} response fields`, -32600);
  }
  if (hasResult && !isJsonValue(message.result)) {
    throw new TransportError(`invalid ${label} response result`, -32600);
  }
  if (hasError) validateRpcError(message.error, label);
  return hasError
    ? { kind: "response", id: message.id, error: message.error }
    : { kind: "response", id: message.id, result: message.result };
}

function validateRpcError(error, label) {
  if (!isObject(error) || !Number.isSafeInteger(error.code) || typeof error.message !== "string" ||
      error.message.length === 0 || error.message.length > 512 || error.message.includes("\0")) {
    throw new TransportError(`invalid ${label} error`, -32600);
  }
  if (hasOwn(error, "data") && !isJsonValue(error.data)) {
    throw new TransportError(`invalid ${label} error data`, -32600);
  }
}

function safeResponseError(code, message) {
  return { code, message };
}

function safeErrorMessage(error, fallback) {
  if (error instanceof TransportError && typeof error.message === "string" && error.message.length <= 128) {
    return error.message;
  }
  return fallback;
}

function encodeFrame(message, label) {
  let encoded;
  try {
    encoded = JSON.stringify(message);
  } catch {
    throw new TransportError(`${label} result is not JSON serializable`, -32603);
  }
  if (encoded === undefined) throw new TransportError(`${label} result is not JSON serializable`, -32603);
  const bytes = Buffer.byteLength(encoded, "utf8") + 1;
  if (bytes > MAX_FRAME_BYTES) throw new TransportError(`${label} frame exceeds size limit`, -32600);
  return { text: `${encoded}\n`, bytes };
}

function withTimeout(promise, timeoutMs) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new TransportError("bounded operation timed out")), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

class FrameWriter {
  constructor(stream, label, onFailure) {
    if (!stream || typeof stream.write !== "function") throw new TypeError(`${label} must be writable`);
    this.stream = stream;
    this.label = label;
    this.onFailure = onFailure;
    this.queue = [];
    this.queuedBytes = 0;
    this.running = null;
    this.closed = false;
    this.ended = false;
    this.failure = null;
    this.stream.on?.("error", () => {
      if (!this.closed) this.fail(new TransportError(`${this.label} write failed`));
    });
  }

  send(message) {
    if (this.closed || this.ended) return Promise.reject(new TransportError(`${this.label} is closed`));
    let frame;
    try {
      frame = encodeFrame(message, this.label);
    } catch (error) {
      this.fail(error);
      return Promise.reject(error);
    }
    if (this.queuedBytes + frame.bytes > MAX_QUEUED_BYTES) {
      const error = new TransportError(`${this.label} output queue is full`);
      this.fail(error);
      return Promise.reject(error);
    }
    this.queuedBytes += frame.bytes;
    const promise = new Promise((resolve, reject) => this.queue.push({ frame, resolve, reject }));
    this.pump();
    return promise;
  }

  pump() {
    if (this.running) return this.running;
    this.running = (async () => {
      while (this.queue.length > 0 && !this.closed) {
        const item = this.queue.shift();
        this.queuedBytes -= item.frame.bytes;
        try {
          const accepted = this.stream.write(item.frame.text, "utf8");
          if (!accepted) await this.waitForDrain();
          item.resolve();
        } catch (error) {
          item.reject(new TransportError(`${this.label} write failed`));
          this.fail(new TransportError(`${this.label} write failed`));
          break;
        }
      }
    })().finally(() => {
      this.running = null;
      if (this.queue.length > 0 && !this.closed) this.pump();
    });
    return this.running;
  }

  waitForDrain() {
    return new Promise((resolve, reject) => {
      let timer;
      const done = (error) => {
        clearTimeout(timer);
        this.stream.off?.("drain", onDrain);
        this.stream.off?.("error", onError);
        if (error) reject(error);
        else resolve();
      };
      const onDrain = () => done();
      const onError = () => done(new TransportError(`${this.label} drain failed`));
      timer = setTimeout(() => done(new TransportError(`${this.label} drain timed out`)), DRAIN_TIMEOUT_MS);
      this.stream.once?.("drain", onDrain);
      this.stream.once?.("error", onError);
    });
  }

  fail(error) {
    if (this.closed) return;
    this.closed = true;
    this.failure = error;
    for (const item of this.queue.splice(0)) {
      this.queuedBytes -= item.frame.bytes;
      item.reject(error);
    }
    try {
      this.onFailure?.(error);
    } catch {
      // A transport failure must not become an uncaught writer callback error.
    }
  }

  abort() {
    if (this.closed) return;
    const callback = this.onFailure;
    this.onFailure = null;
    this.fail(new TransportError(`${this.label} closed`));
    this.onFailure = callback;
  }

  async flush() {
    if (this.running) await this.running;
  }

  async end() {
    if (this.ended) return this.endResult;
    this.ended = true;
    let ok = true;
    try {
      await withTimeout(this.flush(), DRAIN_TIMEOUT_MS);
    } catch {
      ok = false;
    }
    if (typeof this.stream.end !== "function") {
      this.endResult = false;
      return this.endResult;
    }
    await new Promise((resolve) => {
      let settled = false;
      let timer;
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve();
      };
      timer = setTimeout(() => {
        ok = false;
        finish();
      }, DRAIN_TIMEOUT_MS);
      try {
        this.stream.end(finish);
      } catch {
        ok = false;
        finish();
      }
    });
    this.endResult = ok;
    return this.endResult;
  }
}

class LineReader {
  constructor(stream, label, onMessage, onFailure, onEnd) {
    if (!stream || typeof stream.on !== "function") throw new TypeError(`${label} must be readable`);
    this.stream = stream;
    this.label = label;
    this.onMessage = onMessage;
    this.onFailure = onFailure;
    this.onEnd = onEnd;
    this.buffer = Buffer.alloc(0);
    this.stopped = false;
    this.decoder = new TextDecoder("utf-8", { fatal: true });
    this.handleData = (chunk) => this.data(chunk);
    this.handleEnd = () => this.end();
    this.handleClose = () => this.end();
    this.handleError = () => this.fail(new TransportError(`${this.label} read failed`));
    stream.on("data", this.handleData);
    stream.once("end", this.handleEnd);
    stream.once("close", this.handleClose);
    stream.once("error", this.handleError);
  }

  data(chunk) {
    if (this.stopped) return;
    let bytes;
    try {
      bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    } catch {
      this.fail(new TransportError(`${this.label} contained invalid bytes`));
      return;
    }
    if (bytes.length === 0) return;
    let offset = 0;
    while (!this.stopped && offset < bytes.length) {
      const newline = bytes.indexOf(0x0a, offset);
      if (newline < 0) {
        const tail = bytes.subarray(offset);
        if (this.buffer.length + tail.length > MAX_FRAME_BYTES) {
          this.fail(new TransportError(`${this.label} frame exceeds size limit`));
          return;
        }
        this.buffer = Buffer.concat([this.buffer, tail]);
        return;
      }
      const line = bytes.subarray(offset, newline);
      const frameLine = this.buffer.length === 0 ? line : Buffer.concat([this.buffer, line]);
      this.buffer = Buffer.alloc(0);
      offset = newline + 1;
      if (frameLine.length > MAX_FRAME_BYTES) {
        this.fail(new TransportError(`${this.label} frame exceeds size limit`));
        return;
      }
      const frame = frameLine.length > 0 && frameLine[frameLine.length - 1] === 0x0d ? frameLine.subarray(0, frameLine.length - 1) : frameLine;
      if (frame.length === 0 || frame.length + 1 > MAX_FRAME_BYTES) {
        this.fail(new TransportError(`invalid ${this.label} JSON frame`, -32700));
        return;
      }
      let message;
      try {
        message = JSON.parse(this.decoder.decode(frame));
      } catch {
        this.fail(new TransportError(`invalid ${this.label} JSON frame`, -32700));
        return;
      }
      try {
        this.onMessage(message);
      } catch (error) {
        this.fail(error instanceof TransportError ? error : new TransportError(`invalid ${this.label} JSON-RPC message`, -32600));
        return;
      }
    }
  }

  end() {
    if (this.stopped) return;
    if (this.buffer.length > 0) {
      this.fail(new TransportError(`${this.label} ended with an incomplete frame`, -32700));
      return;
    }
    this.stopped = true;
    try {
      this.onEnd?.();
    } catch {
      // Shutdown is initiated by the owner after a reader end.
    }
  }

  fail(error) {
    if (this.stopped) return;
    this.stopped = true;
    try {
      this.onFailure?.(error);
    } catch {
      // Preserve fail-closed behavior if the callback itself fails.
    }
  }

  stop() {
    this.stopped = true;
    this.stream.pause?.();
  }
}

function validateOptions(options) {
  if (!isObject(options)) throw new TypeError("bridgeOptions must be an object");
  for (const key of ["policy", "model", "effort", "instructions", "configSnapshot"]) {
    if (!hasOwn(options, key)) throw new TypeError(`bridgeOptions is missing ${key}`);
  }
}

function validateRuntimeInterfaces(child, input, output) {
  if (!child || typeof child.on !== "function" || typeof child.once !== "function" ||
      typeof child.kill !== "function" || !child.stdin || typeof child.stdin.write !== "function" ||
      typeof child.stdin.end !== "function" || typeof child.stdin.on !== "function" || typeof child.stdout?.on !== "function" ||
      typeof child.stdout?.once !== "function") {
    throw new TypeError("child must expose spawned ChildProcess stdio and lifecycle interfaces");
  }
  if (child.stderr !== null && child.stderr !== undefined && typeof child.stderr.on !== "function") {
    throw new TypeError("child stderr must be readable when present");
  }
  if (!input || typeof input.on !== "function" || typeof input.once !== "function") {
    throw new TypeError("input must be a readable stream");
  }
  if (!output || typeof output.write !== "function" || typeof output.end !== "function") {
    throw new TypeError("output must be a writable stream");
  }
}

async function cleanupSetupChild(child) {
  try {
    await stopChild(child, childExitState(child));
  } catch {
    // The original setup error remains authoritative; cleanup is bounded.
  }
}

function childExitState(child) {
  const code = child.exitCode ?? null;
  const signal = child.signalCode ?? null;
  return {
    exited: code !== null || signal !== null,
    code,
    signal,
    error: false,
  };
}

function waitForChildExit(child, state, timeoutMs) {
  if (state.exited) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const onError = () => {
      state.error = true;
    };
    const finish = (code = null, signal = null) => {
      if (settled) return;
      settled = true;
      state.exited = true;
      state.code = code ?? child.exitCode ?? null;
      state.signal = signal ?? child.signalCode ?? null;
      clearTimeout(timer);
      child.off?.("exit", finish);
      child.off?.("close", finish);
      child.off?.("error", onError);
      resolve(true);
    };
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.off?.("exit", finish);
      child.off?.("close", finish);
      child.off?.("error", onError);
      resolve(false);
    }, timeoutMs);
    child.once?.("exit", finish);
    child.once?.("close", finish);
    child.once?.("error", onError);
  });
}

async function stopChild(child, state) {
  try {
    if (child.stdin && !child.stdin.destroyed && !child.stdin.writableEnded) child.stdin.end();
  } catch {
    // The child may have exited before stdin was closed.
  }
  let exited = await waitForChildExit(child, state, CHILD_GRACE_MS);
  if (!exited) {
    try {
      if (!state.exited) child.kill("SIGTERM");
    } catch {
      // Preserve the bounded cleanup result below.
    }
    exited = await waitForChildExit(child, state, CHILD_TERM_GRACE_MS);
  }
  if (!exited) {
    try {
      if (!state.exited) child.kill("SIGKILL");
    } catch {
      // Preserve the bounded cleanup result below.
    }
    exited = await waitForChildExit(child, state, CHILD_KILL_GRACE_MS);
  }
  return exited;
}

export async function runTransport({ child, input, output, bridgeOptions }) {
  try {
    validateRuntimeInterfaces(child, input, output);
    validateOptions(bridgeOptions);
  } catch (error) {
    await cleanupSetupChild(child);
    throw error;
  }

  let shuttingDown = false;
  let fatalReason = null;
  let shutdownPromise;
  let resolveShutdownReady;
  const shutdownReady = new Promise((resolve) => {
    resolveShutdownReady = resolve;
  });
  let parentReader;
  let childReader;
  let childInitialized = false;
  let initializeInFlight = false;
  let initializeCompleted = false;
  let initializedNotificationSeen = false;
  let parentIdSeen = new Set();
  const parentPending = new Map();
  const childPending = new Map();
  const serverPending = new Map();
  const serverIdSeen = new Set();
  const queuedServerRequests = [];
  let turnStartChildPending = false;
  let turnStartChildFailed = false;
  let childRequestSequence = 0;
  let removeSignalHandlers = () => {};
  const exit = childExitState(child);
  const exitPromise = new Promise((resolve) => {
    const finish = (code = null, signal = null) => {
      if (exit.exited) return;
      exit.exited = true;
      exit.code = code ?? child.exitCode ?? null;
      exit.signal = signal ?? child.signalCode ?? null;
      resolve(exit);
    };
    if (exit.exited) resolve(exit);
    else {
      child.once("exit", (code, signal) => finish(code, signal));
      child.once("close", (code, signal) => finish(code, signal));
      child.once("error", () => {
        exit.error = true;
      });
    }
  });

  const beginShutdown = (reason, { fatal = false } = {}) => {
    if (fatal && fatalReason === null) fatalReason = reason;
    if (shutdownPromise) return shutdownPromise;
    shuttingDown = true;
    parentReader?.stop();
    childReader?.stop();
    for (const entry of parentPending.values()) entry.reject(new TransportError("transport closed"));
    parentPending.clear();
    for (const entry of childPending.values()) entry.reject(new TransportError("transport closed"));
    childPending.clear();
    serverPending.clear();
    queuedServerRequests.length = 0;
    shutdownPromise = (async () => {
      childWriter.abort();
      const stopped = await stopChild(child, exit);
      let outputOk = true;
      try {
        await withTimeout(parentWriter.flush(), DRAIN_TIMEOUT_MS);
      } catch {
        outputOk = false;
      }
      if (!await parentWriter.end()) outputOk = false;
      if (parentWriter.failure) outputOk = false;
      if (stopped) await exitPromise;
      if (!outputOk && fatalReason === null) fatalReason = "parent output shutdown failed";
      const result = {
        ok: fatalReason === null && stopped && exit.exited && !exit.error && exit.code === 0 && exit.signal === null,
        reason: fatalReason ?? reason,
        childExitCode: exit.code,
        childSignal: exit.signal,
        childError: exit.error,
        childStopped: stopped,
        outputOk,
        unexpectedChildExit: (typeof fatalReason === "string" && fatalReason.startsWith("child exited")) ||
          (fatalReason === null && (!exit.exited || exit.error || exit.code !== 0)),
        exitCode: fatalReason === null && stopped && exit.exited && !exit.error && exit.code === 0 && exit.signal === null ? 0 : 1,
      };
      removeSignalHandlers();
      return result;
    })();
    shutdownPromise.then(resolveShutdownReady, () => {
      if (fatalReason === null) fatalReason = "transport shutdown failed";
      resolveShutdownReady({ ok: false, reason: fatalReason, childStopped: false, outputOk: false, exitCode: 1 });
    });
    return shutdownPromise;
  };

  const failTransport = (reason) => {
    const message = typeof reason === "string" ? reason : "transport failure";
    void beginShutdown(message, { fatal: true });
  };

  const parentWriter = new FrameWriter(output, "parent output", failTransport);
  const childWriter = new FrameWriter(child.stdin, "child input", failTransport);

  let bridge;
  try {
    bridge = new CodexScopeBridge(bridgeOptions, requestChild);
  } catch (error) {
    failTransport("bridge initialization failed");
    return await shutdownPromise;
  }

  function pendingCount() {
    return parentPending.size + childPending.size + serverPending.size;
  }

  function sendParentError(id, code, message) {
    if (shuttingDown) return Promise.resolve();
    return parentWriter.send({ id, error: safeResponseError(code, message) }).catch(() => {});
  }

  function requestChild(method, params) {
    if (shuttingDown) return Promise.reject(new TransportError("transport closed"));
    if (pendingCount() >= MAX_PENDING) {
      failTransport("pending request limit exceeded");
      return Promise.reject(new TransportError("pending request limit exceeded"));
    }
    const id = `${CHILD_ID_PREFIX}${++childRequestSequence}`;
    const key = idKey(id);
    let rejectPending;
    const pending = new Promise((resolve, reject) => {
      rejectPending = reject;
      childPending.set(key, { resolve, reject, method });
    });
    if (method === "turn/start") turnStartChildPending = true;
    childWriter.send({ id, method, params }).catch(() => {
      if (childPending.delete(key)) {
        rejectPending(new TransportError("child request could not be sent"));
      }
      failTransport("child request could not be sent");
    });
    return pending;
  }

  async function dispatchParentRequest(request) {
    const key = idKey(request.id);
    if (request.method === "initialize" && (initializeInFlight || initializeCompleted)) {
      await sendParentError(request.id, -32600, "initialize may only be requested once");
      failTransport("duplicate initialize request");
      return;
    }
    if (parentPending.has(key)) {
      await sendParentError(request.id, -32600, "duplicate request id");
      failTransport("duplicate parent request id");
      return;
    }
    if (pendingCount() >= MAX_PENDING) {
      await sendParentError(request.id, -32000, "pending request limit exceeded");
      failTransport("pending request limit exceeded");
      return;
    }
    if (parentIdSeen.size >= MAX_SEEN_IDS) {
      await sendParentError(request.id, -32000, "request id budget exceeded");
      failTransport("request id budget exceeded");
      return;
    }
    parentIdSeen.add(key);
    parentPending.set(key, { reject: () => {} });
    if (request.method === "initialize") initializeInFlight = true;
    try {
      const result = await bridge.handleClientRequest(request.method, request.params);
      parentPending.delete(key);
      if (request.method === "initialize") {
        await parentWriter.send({ id: request.id, result });
        initializeInFlight = false;
        initializeCompleted = true;
        childInitialized = true;
      } else {
        await parentWriter.send({ id: request.id, result });
      }
    } catch (error) {
      parentPending.delete(key);
      const message = safeErrorMessage(error, "bridge request rejected");
      const code = Number.isSafeInteger(error?.code) ? error.code : -32000;
      await sendParentError(request.id, code, message);
      if (request.method === "initialize") initializeInFlight = false;
      if (request.method === "turn/start") turnStartChildFailed = true;
    } finally {
      if (request.method === "turn/start") flushQueuedServerRequests();
    }
  }

  async function dispatchChildRequest(request, alreadyRegistered = false) {
    const key = idKey(request.id);
    if (!alreadyRegistered && (key.startsWith(`s:${CHILD_ID_PREFIX}`) || serverIdSeen.has(key) || serverPending.has(key) || childPending.has(key))) {
      failTransport("duplicate child request id");
      return;
    }
    if (!alreadyRegistered && pendingCount() >= MAX_PENDING) {
      failTransport("pending request limit exceeded");
      return;
    }
    if (!alreadyRegistered && serverIdSeen.size >= MAX_SEEN_IDS) {
      failTransport("server request id budget exceeded");
      return;
    }
    if (!alreadyRegistered) {
      serverIdSeen.add(key);
      serverPending.set(key, true);
    }
    try {
      const result = await bridge.handleServerRequest(request.method, request.params);
      serverPending.delete(key);
      if (!shuttingDown) await childWriter.send({ id: request.id, result });
    } catch {
      serverPending.delete(key);
      if (!shuttingDown) {
        await childWriter.send({
          id: request.id,
          error: safeResponseError(-32000, "server request rejected"),
        }).catch(() => {});
      }
    }
  }

  function flushQueuedServerRequests() {
    if (shuttingDown) return;
    if (bridge.turnStartPending && !turnStartChildFailed) return;
    const queued = queuedServerRequests.splice(0);
    for (const request of queued) void dispatchChildRequest(request, true);
  }

  function dispatchChildNotification(notification) {
    let forwarded;
    try {
      forwarded = bridge.handleNotification(notification.method, notification.params);
    } catch {
      failTransport("child notification rejected");
      return;
    }
    if (forwarded === undefined || shuttingDown) return;
    const frame = { method: notification.method };
    if (forwarded !== undefined) frame.params = forwarded;
    parentWriter.send(frame).catch(() => {});
  }

  function handleChildMessage(message) {
    const frame = validateRpcMessage(message, "child");
    if (frame.kind === "response") {
      const key = idKey(frame.id);
      const pending = childPending.get(key);
      if (!pending) {
        failTransport("unknown child response id");
        return;
      }
      childPending.delete(key);
      if (frame.error) pending.reject(new TransportError("child request failed"));
      else pending.resolve(frame.result);
      if (pending.method === "turn/start") {
        turnStartChildPending = false;
        turnStartChildFailed = Boolean(frame.error);
      }
      return;
    }
    if (frame.kind === "request") {
      if ((turnStartChildPending || bridge.turnStartPending) && !turnStartChildFailed) {
        const key = idKey(frame.id);
        if (key.startsWith(`s:${CHILD_ID_PREFIX}`) || serverIdSeen.has(key) || serverPending.has(key) || childPending.has(key)) {
          failTransport("duplicate child request id");
          return;
        }
        if (pendingCount() >= MAX_PENDING) {
          failTransport("pending request limit exceeded");
          return;
        }
        if (serverIdSeen.size >= MAX_SEEN_IDS) {
          failTransport("server request id budget exceeded");
          return;
        }
        serverIdSeen.add(key);
        serverPending.set(key, true);
        queuedServerRequests.push(frame);
      } else {
        void dispatchChildRequest(frame);
      }
      return;
    }
    dispatchChildNotification(frame);
  }

  function handleParentMessage(message) {
    const frame = validateRpcMessage(message, "parent");
    if (frame.kind === "request") {
      const key = idKey(frame.id);
      if (parentIdSeen.has(key)) {
        void sendParentError(frame.id, -32600, "duplicate request id");
        failTransport("duplicate parent request id");
        return;
      }
      void dispatchParentRequest(frame);
      return;
    }
    if (frame.method !== "initialized") {
      failTransport("unsupported parent notification");
      return;
    }
    if (initializedNotificationSeen || !initializeCompleted || initializeInFlight || !childInitialized) {
      failTransport("initialized notification arrived before initialize completed");
      return;
    }
    initializedNotificationSeen = true;
    childWriter.send({ method: "initialized", ...(hasOwn(message, "params") ? { params: frame.params } : {}) }).catch(() => {});
  }

  try {
    parentReader = new LineReader(input, "parent input", handleParentMessage, (error) => {
    const parseError = error.code === -32700;
    void sendParentError(null, parseError ? -32700 : -32600, parseError ? "parse error" : "invalid request");
    failTransport(error.message);
    }, () => {
      void beginShutdown("upstream EOF");
    });
    childReader = new LineReader(child.stdout, "child output", handleChildMessage, (error) => {
      failTransport(error.message);
    }, () => {
      failTransport("child output EOF");
    });
    child.stderr?.on?.("data", () => {});
    child.stderr?.on?.("error", () => {});
    child.on("error", () => {
      if (!shuttingDown) failTransport("child process failed");
    });
    child.on("exit", (code, signal) => {
      if (!shuttingDown) failTransport(`child exited unexpectedly (${code ?? "null"}/${signal ?? "null"})`);
    });

    const onSignal = (signal) => {
      failTransport(`received ${signal}`);
    };
    const onSigterm = () => onSignal("SIGTERM");
    const onSigint = () => onSignal("SIGINT");
    process.once("SIGTERM", onSigterm);
    process.once("SIGINT", onSigint);
    removeSignalHandlers = () => {
      process.off("SIGTERM", onSigterm);
      process.off("SIGINT", onSigint);
    };
    if (exit.exited) failTransport(`child exited unexpectedly (${exit.code ?? "null"}/${exit.signal ?? "null"})`);
  } catch {
    failTransport("transport setup failed");
    return await shutdownPromise;
  }

  return await shutdownReady;
}
