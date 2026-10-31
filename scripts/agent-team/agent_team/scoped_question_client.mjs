#!/usr/bin/env node

import fs from "node:fs";
import net from "node:net";
import path from "node:path";

export const MAX_QUESTIONS = 4;
export const MAX_QUESTION_CHARS = 20_000;
export const MAX_FRAME_BYTES = 512 * 1024;
export const MAX_BATCHES_PER_TURN = 64;
const MAX_ID_CHARS = 256;
const CONNECT_TIMEOUT_MS = 2_000;
const WRITE_TIMEOUT_MS = 2_000;
const OPTION_META_KEY = "_claude/askUserQuestionOption";
const CUSTOM_ANSWER_META_KEY = "_askUserQuestionCustomAnswer";

function fail(message) {
  throw new Error(`scoped question client: ${message}`);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, allowed, label) {
  if (!isObject(value)) fail(`${label} must be an object`);
  const allowedSet = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!allowedSet.has(key)) fail(`${label} has unknown field ${key}`);
  }
}

function text(value, label) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    fail(`${label} must be a non-empty string without NUL`);
  }
  if ([...value].length > MAX_QUESTION_CHARS) {
    fail(`${label} exceeds ${MAX_QUESTION_CHARS} characters`);
  }
  return value;
}

function answerText(value, label) {
  const result = text(value, label);
  if (result.trim().length === 0) fail(`${label} must not be blank`);
  return result;
}

function identifier(value, label) {
  const result = text(value, label);
  if ([...result].length > MAX_ID_CHARS) fail(`${label} exceeds ${MAX_ID_CHARS} characters`);
  if ([...result].some((character) => {
    const code = character.codePointAt(0);
    return code !== undefined && (code < 0x20 || code === 0x7f);
  })) {
    fail(`${label} contains a control character`);
  }
  return result;
}

function currentUid() {
  const uid = process.getuid?.();
  if (!Number.isSafeInteger(uid)) fail("current uid is unavailable");
  return uid;
}

function canonicalSocketPath(rawPath) {
  if (typeof rawPath !== "string" || rawPath.length === 0 || rawPath.includes("\0")) {
    fail("question socket must be a non-empty absolute path without NUL");
  }
  if (!path.isAbsolute(rawPath)) fail("question socket must be absolute");
  const absolute = path.resolve(rawPath);
  let socketInfo;
  try {
    socketInfo = fs.lstatSync(absolute);
  } catch (error) {
    if (error?.code === "ENOENT") fail("question socket does not exist");
    fail(`question socket is unavailable: ${error?.message ?? error}`);
  }
  if (socketInfo.isSymbolicLink() || !socketInfo.isSocket()) {
    fail("question socket must be an existing non-symlink Unix socket");
  }
  const uid = currentUid();
  if (socketInfo.uid !== uid) fail("question socket must be owned by the current user");
  const canonical = fs.realpathSync.native(absolute);
  const parent = path.dirname(canonical);
  let parentInfo;
  try {
    parentInfo = fs.lstatSync(parent);
  } catch (error) {
    fail(`question socket parent is unavailable: ${error?.message ?? error}`);
  }
  if (parentInfo.isSymbolicLink() || !parentInfo.isDirectory()) {
    fail("question socket parent must be a real directory");
  }
  if (parentInfo.uid !== uid || (parentInfo.mode & 0o777) !== 0o700) {
    fail("question socket parent must be current-user-owned mode 0700");
  }
  if (fs.realpathSync.native(parent) !== parent) fail("question socket parent must be its canonical real path");
  return canonical;
}

export function validateQuestionSocket(rawPath) {
  return canonicalSocketPath(rawPath);
}

function validateOption(rawOption, label) {
  exactKeys(rawOption, ["const", "title", "description", "_meta"], label);
  const optionValue = text(rawOption.const, `${label}.const`);
  const title = text(rawOption.title, `${label}.title`);
  if (rawOption.description !== undefined) {
    text(rawOption.description, `${label}.description`);
  }
  if (rawOption._meta !== undefined) {
    exactKeys(rawOption._meta, [OPTION_META_KEY], `${label}._meta`);
    const optionMeta = rawOption._meta[OPTION_META_KEY];
    exactKeys(optionMeta, ["preview"], `${label}._meta.${OPTION_META_KEY}`);
    text(optionMeta.preview, `${label}._meta.${OPTION_META_KEY}.preview`);
  }
  return {
    value: optionValue,
    title,
    description: rawOption.description,
    preview: rawOption._meta?.[OPTION_META_KEY]?.preview,
  };
}

function validateSelectProperty(rawProperty, index) {
  const label = `requestedSchema.properties.question_${index}`;
  if (!isObject(rawProperty)) fail(`${label} must be an object`);
  const isMulti = rawProperty.type === "array";
  if (!isMulti) {
    exactKeys(rawProperty, ["type", "title", "description", "oneOf", "_meta"], label);
    if (rawProperty.type !== "string") fail(`${label}.type must be string`);
    if (!Array.isArray(rawProperty.oneOf) || rawProperty.oneOf.length === 0) {
      fail(`${label}.oneOf must be a non-empty array`);
    }
    return {
      multiSelect: false,
      title: rawProperty.title === undefined ? undefined : text(rawProperty.title, `${label}.title`),
      description: rawProperty.description === undefined
        ? undefined
        : text(rawProperty.description, `${label}.description`),
      options: rawProperty.oneOf.map((option, optionIndex) =>
        validateOption(option, `${label}.oneOf[${optionIndex}]`),
      ),
    };
  }

  exactKeys(rawProperty, ["type", "title", "description", "items", "_meta"], label);
  if (!isObject(rawProperty.items)) fail(`${label}.items must be an object`);
  exactKeys(rawProperty.items, ["type", "anyOf", "_meta"], `${label}.items`);
  if (rawProperty.items.type !== undefined && rawProperty.items.type !== "string") {
    fail(`${label}.items.type must be string`);
  }
  if (!Array.isArray(rawProperty.items.anyOf) || rawProperty.items.anyOf.length === 0) {
    fail(`${label}.items.anyOf must be a non-empty array`);
  }
  return {
    multiSelect: true,
    title: rawProperty.title === undefined ? undefined : text(rawProperty.title, `${label}.title`),
    description: rawProperty.description === undefined
      ? undefined
      : text(rawProperty.description, `${label}.description`),
    options: rawProperty.items.anyOf.map((option, optionIndex) =>
      validateOption(option, `${label}.items.anyOf[${optionIndex}]`),
    ),
  };
}

function validateCustomProperty(rawProperty, index) {
  const label = `requestedSchema.properties.question_${index}_custom`;
  if (!isObject(rawProperty)) fail(`${label} must be an object`);
  exactKeys(rawProperty, ["type", "title", "description", "_meta"], label);
  if (rawProperty.type !== "string") fail(`${label}.type must be string`);
  if (rawProperty.title !== undefined) text(rawProperty.title, `${label}.title`);
  if (rawProperty.description !== undefined) text(rawProperty.description, `${label}.description`);
  exactKeys(rawProperty._meta, [CUSTOM_ANSWER_META_KEY], `${label}._meta`);
  const marker = rawProperty._meta[CUSTOM_ANSWER_META_KEY];
  exactKeys(marker, ["questionId", "isCustomAnswer"], `${label}._meta.${CUSTOM_ANSWER_META_KEY}`);
  if (marker.questionId !== `question_${index}` || marker.isCustomAnswer !== true) {
    fail(`${label} has an invalid custom-answer companion marker`);
  }
}

function validateSchema(rawSchema) {
  if (!isObject(rawSchema)) fail("requestedSchema must be an object");
  exactKeys(rawSchema, ["type", "title", "properties", "required", "description", "_meta"], "requestedSchema");
  if (rawSchema.type !== undefined && rawSchema.type !== "object") {
    fail("requestedSchema.type must be object");
  }
  if (!isObject(rawSchema.properties)) fail("requestedSchema.properties must be an object");
  if (rawSchema.title !== undefined) text(rawSchema.title, "requestedSchema.title");
  if (rawSchema.description !== undefined) text(rawSchema.description, "requestedSchema.description");
  if (rawSchema.required !== undefined) {
    if (!Array.isArray(rawSchema.required) || rawSchema.required.some((item) => typeof item !== "string")) {
      fail("requestedSchema.required must be an array of strings");
    }
  }

  const propertyNames = Object.keys(rawSchema.properties);
  const indices = new Set();
  for (const name of propertyNames) {
    const match = /^question_(\d+)(_custom)?$/.exec(name);
    if (!match) fail(`requestedSchema.properties has unknown field ${name}`);
    const numericIndex = Number(match[1]);
    if (!Number.isSafeInteger(numericIndex)) fail(`requestedSchema.properties has unknown field ${name}`);
    const canonical = `question_${numericIndex}${match[2] ?? ""}`;
    if (name !== canonical) fail(`requestedSchema.properties has unknown field ${name}`);
    indices.add(numericIndex);
  }
  const ordered = [...indices].sort((left, right) => left - right);
  if (ordered.length === 0 || ordered.length > MAX_QUESTIONS) {
    fail(`requestedSchema must contain one to ${MAX_QUESTIONS} questions`);
  }
  for (const [position, index] of ordered.entries()) {
    if (index !== position) fail("question form fields must use contiguous indexes");
    const selectName = `question_${index}`;
    const customName = `${selectName}_custom`;
    if (!Object.hasOwn(rawSchema.properties, selectName) || !Object.hasOwn(rawSchema.properties, customName)) {
      fail(`requestedSchema must contain ${selectName} and ${customName}`);
    }
    validateSelectProperty(rawSchema.properties[selectName], index);
    validateCustomProperty(rawSchema.properties[customName], index);
  }
  if (rawSchema.required !== undefined) {
    const known = new Set(propertyNames);
    if (rawSchema.required.some((item) => !known.has(item))) {
      fail("requestedSchema.required contains an unknown field");
    }
  }
  return ordered;
}

function optionLines(options) {
  return options.map((option) => {
    let line = `- ${option.title}`;
    if (option.value !== option.title) line += ` (value: ${option.value})`;
    if (option.description !== undefined) line += ` — ${option.description}`;
    if (option.preview !== undefined) line += `\n  Preview: ${option.preview}`;
    return line;
  });
}

export function formatQuestionForm(rawParams) {
  if (!isObject(rawParams)) fail("elicitation request must be an object");
  exactKeys(rawParams, ["mode", "sessionId", "toolCallId", "message", "requestedSchema", "_meta"], "elicitation request");
  if (rawParams.mode !== "form") fail("only form elicitations are supported");
  const sessionId = identifier(rawParams.sessionId, "sessionId");
  const toolCallId = identifier(rawParams.toolCallId, "toolCallId");
  const message = text(rawParams.message, "message");
  const indices = validateSchema(rawParams.requestedSchema);
  const multiple = indices.length > 1;
  const questions = indices.map((index) => {
    const select = validateSelectProperty(rawParams.requestedSchema.properties[`question_${index}`], index);
    const description = multiple
      ? text(select.description, `question_${index}.description`)
      : select.description;
    const questionText = description === undefined || description === message
      ? message
      : `Context: ${message}\nQuestion: ${description}`;
    const lines = [];
    if (select.title !== undefined) lines.push(`Header: ${select.title}`);
    lines.push(questionText, "Options:", ...optionLines(select.options));
    const body = lines.join("\n");
    text(body, `question_${index} body`);
    return {
      field: `question_${index}_custom`,
      body,
    };
  });
  const result = { sessionId, toolCallId, fields: questions.map((question) => question.field), questions };
  const frame = Buffer.from(JSON.stringify({
    kind: "question",
    session_id: sessionId,
    tool_call_id: toolCallId,
    questions,
  }) + "\n", "utf8");
  if (frame.length > MAX_FRAME_BYTES) fail(`question frame exceeds ${MAX_FRAME_BYTES} bytes`);
  return result;
}

export function validateQuestionForm(rawParams) {
  return formatQuestionForm(rawParams);
}

function encodeFrame(value, label) {
  const frame = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
  if (frame.length > MAX_FRAME_BYTES) fail(`${label} exceeds ${MAX_FRAME_BYTES} bytes`);
  return frame;
}

function waitForAbort(signal, onAbort) {
  if (!signal) return () => {};
  if (signal.aborted) {
    onAbort();
    return () => {};
  }
  signal.addEventListener("abort", onAbort, { once: true });
  return () => signal.removeEventListener("abort", onAbort);
}

function connectSocket(socketPath, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("question channel aborted"));
      return;
    }
    const socket = net.createConnection({ path: socketPath });
    let settled = false;
    let cleanupAbort = () => {};
    const finish = (error, value = undefined) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanupAbort();
      socket.off("connect", onConnect);
      socket.off("error", onError);
      if (error) {
        socket.destroy();
        reject(error);
      } else {
        resolve(value);
      }
    };
    const onConnect = () => finish(undefined, socket);
    const onError = (error) => finish(new Error(`question socket connection failed: ${error?.message ?? error}`));
    const timer = setTimeout(() => finish(new Error("question socket connection timed out")), CONNECT_TIMEOUT_MS);
    cleanupAbort = waitForAbort(signal, () => {
      socket.destroy();
      finish(new Error("question channel aborted"));
    });
    if (settled) return;
    socket.once("connect", onConnect);
    socket.once("error", onError);
  });
}

function writeFrame(socket, frame, signal, end = false) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let cleanupAbort = () => {};
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanupAbort();
      socket.off("error", onError);
      if (error) reject(error);
      else resolve();
    };
    const onError = (error) => finish(new Error(`question socket write failed: ${error?.message ?? error}`));
    const timer = setTimeout(() => finish(new Error("question socket write timed out")), WRITE_TIMEOUT_MS);
    cleanupAbort = waitForAbort(signal, () => {
      socket.destroy();
      finish(new Error("question channel aborted"));
    });
    if (settled) return;
    socket.once("error", onError);
    if (end) socket.end(frame, () => finish());
    else socket.write(frame, () => finish());
  });
}

function readFrame(socket, signal) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    let settled = false;
    let cleanupAbort = () => {};
    const finish = (error, value = undefined) => {
      if (settled) return;
      settled = true;
      cleanupAbort();
      socket.off("data", onData);
      socket.off("end", onEnd);
      socket.off("close", onClose);
      socket.off("error", onError);
      if (error) reject(error);
      else resolve(value);
    };
    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, Buffer.from(chunk)]);
      if (buffer.length > MAX_FRAME_BYTES) {
        finish(new Error(`question response exceeds ${MAX_FRAME_BYTES} bytes`));
        socket.destroy();
        return;
      }
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) return;
      if (newline === 0 || newline + 1 !== buffer.length) {
        finish(new Error("question response must contain exactly one JSONL frame"));
        socket.destroy();
        return;
      }
      let parsed;
      try {
        const line = buffer.subarray(0, newline).toString("utf8");
        if (line !== line.trim()) fail("question response has invalid surrounding whitespace");
        parsed = JSON.parse(line);
      } catch (error) {
        finish(new Error(`question response JSON is invalid: ${error?.message ?? error}`));
        socket.destroy();
        return;
      }
      finish(undefined, parsed);
    };
    const onEnd = () => finish(new Error("question socket closed before an answer"));
    const onClose = () => finish(new Error("question socket disconnected before an answer"));
    const onError = (error) => finish(new Error(`question socket read failed: ${error?.message ?? error}`));
    cleanupAbort = waitForAbort(signal, () => finish(new Error("question channel aborted")));
    if (settled) return;
    socket.on("data", onData);
    socket.once("end", onEnd);
    socket.once("close", onClose);
    socket.once("error", onError);
  });
}

function validateAnswer(rawAnswer, fields) {
  exactKeys(rawAnswer, ["kind", "answers"], "question answer");
  if (rawAnswer.kind !== "answer") fail("question answer kind must be answer");
  if (!isObject(rawAnswer.answers)) fail("question answer answers must be an object");
  const answerKeys = Object.keys(rawAnswer.answers);
  if (answerKeys.length !== fields.length || fields.some((field) => !Object.hasOwn(rawAnswer.answers, field))) {
    fail("question answer fields do not match the requested questions");
  }
  const answers = {};
  for (const field of fields) answers[field] = answerText(rawAnswer.answers[field], `question answer ${field}`);
  return answers;
}

function validateRecorded(rawRecorded, sessionId, toolCallId) {
  exactKeys(rawRecorded, ["kind", "session_id", "tool_call_id"], "question receipt confirmation");
  if (
    rawRecorded.kind !== "recorded" ||
    rawRecorded.session_id !== sessionId ||
    rawRecorded.tool_call_id !== toolCallId
  ) {
    fail("question receipt confirmation does not match the active request");
  }
}

export class ScopedQuestionClient {
  constructor(socketPath) {
    const canonical = canonicalSocketPath(socketPath);
    if (!canonical) fail("question socket is absent");
    this.socketPath = canonical;
    this.boundSessionId = undefined;
    this.sessions = new Map();
    this.sockets = new Set();
    this.closed = false;
  }

  async request(rawParams, { signal, observedToolCall = false } = {}) {
    if (this.closed) fail("question client is closed");
    if (!observedToolCall) fail("question tool call was not observed");
    const form = formatQuestionForm(rawParams);
    if (this.boundSessionId !== undefined && this.boundSessionId !== form.sessionId) {
      fail("question request session does not match the active session");
    }
    this.boundSessionId ??= form.sessionId;
    const state = this.sessions.get(form.sessionId) ?? { active: false, batches: 0 };
    if (state.active) fail("a question batch is already outstanding for this session");
    if (state.batches >= MAX_BATCHES_PER_TURN) {
      fail(`question batch limit ${MAX_BATCHES_PER_TURN} exceeded`);
    }
    state.active = true;
    state.batches += 1;
    this.sessions.set(form.sessionId, state);
    let socket;
    try {
      const canonical = canonicalSocketPath(this.socketPath);
      if (!canonical) fail("question socket disappeared");
      socket = await connectSocket(canonical, signal);
      this.sockets.add(socket);
      const questionFrame = encodeFrame({
        kind: "question",
        session_id: form.sessionId,
        tool_call_id: form.toolCallId,
        questions: form.questions,
      }, "question frame");
      await writeFrame(socket, questionFrame, signal);
      const rawAnswer = await readFrame(socket, signal);
      const answers = validateAnswer(rawAnswer, form.fields);
      const receipt = encodeFrame({
        kind: "received",
        session_id: form.sessionId,
        tool_call_id: form.toolCallId,
      }, "question receipt");
      await writeFrame(socket, receipt, signal);
      const recorded = await readFrame(socket, signal);
      validateRecorded(recorded, form.sessionId, form.toolCallId);
      return { action: "accept", content: answers };
    } finally {
      if (socket) {
        this.sockets.delete(socket);
        socket.destroy();
      }
      state.active = false;
    }
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
  }
}
