#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { TextDecoder } from "node:util";
import { decideTool, inspectTarget, parsePolicy } from "./scoped_policy.mjs";

const MAX_REGULAR_FILE_BYTES = 10_000_000;
const MAX_READ_RESULT_BYTES = 1_048_576;
const MAX_TOOL_RESULT_BYTES = 1_048_576;
const MAX_VISITED_ENTRIES = 5_000;
const READ_LINE_LIMIT = 2_000;
const LIST_LIMIT = 1_000;
const IO_CHUNK_BYTES = 64 * 1024;

const textDecoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
let temporaryCounter = 0;

function fail(message) {
  throw new Error(`scoped file tools: ${message}`);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function normalizedPolicy(rawPolicy) {
  return parsePolicy(rawPolicy);
}

function assertExactObject(value, fields, label) {
  if (!isObject(value)) fail(`${label} must be an object`);
  const expected = new Set(fields);
  const actual = Object.keys(value);
  if (actual.length !== expected.size || actual.some((field) => !expected.has(field))) {
    fail(`${label} fields must be exactly ${fields.join(", ")}`);
  }
  return value;
}

function assertString(value, field, { nonEmpty = false } = {}) {
  if (typeof value !== "string") fail(`${field} must be a string`);
  if (nonEmpty && value.length === 0) fail(`${field} must not be empty`);
  if (value.includes("\0")) fail(`${field} must not contain NUL`);
  return value;
}

function assertAbsolutePath(value, field) {
  const candidate = assertString(value, field, { nonEmpty: true });
  if (!path.isAbsolute(candidate)) fail(`${field} must be an absolute path`);
  if ([...candidate].some((character) => {
    const code = character.codePointAt(0);
    return code !== undefined && code < 0x20;
  })) {
    fail(`${field} must not contain control characters`);
  }
  return candidate;
}

function assertInteger(value, field, minimum, maximum) {
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    fail(`${field} must be an integer from ${minimum} to ${maximum}`);
  }
  return value;
}

function assertBoolean(value, field) {
  if (typeof value !== "boolean") fail(`${field} must be boolean`);
  return value;
}

function assertTextString(value, field, { nonEmpty = false } = {}) {
  const text = assertString(value, field, { nonEmpty });
  // Buffer encodes lone UTF-16 surrogates as U+FFFD. Reject that lossy path.
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) fail(`${field} must be valid UTF-8 text`);
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      fail(`${field} must be valid UTF-8 text`);
    }
  }
  return text;
}

function requireDecision(policy, toolName, filePath) {
  const decision = decideTool(policy, toolName, { file_path: filePath });
  if (!isObject(decision) || decision.behavior !== "allow") {
    const message = isObject(decision) && typeof decision.message === "string"
      ? decision.message
      : "shared policy denied the operation";
    fail(message);
  }
  return decision;
}

function inspect(policy, filePath, options) {
  const target = inspectTarget(policy, filePath, options);
  if (!isObject(target) || target.ok !== true || typeof target.path !== "string") {
    const reason = isObject(target) && typeof target.reason === "string"
      ? target.reason
      : "shared policy denied the target";
    fail(reason);
  }
  return target;
}

function currentUid() {
  if (typeof process.getuid !== "function") fail("current user identity is unavailable");
  return process.getuid();
}

function noFollowFlag() {
  if (typeof fs.constants.O_NOFOLLOW !== "number") {
    fail("O_NOFOLLOW is unavailable on this platform");
  }
  return fs.constants.O_NOFOLLOW;
}

function nonBlockFlag() {
  if (typeof fs.constants.O_NONBLOCK !== "number") {
    fail("O_NONBLOCK is unavailable on this platform");
  }
  return fs.constants.O_NONBLOCK;
}

function statIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    uid: stats.uid,
    nlink: stats.nlink,
    type: stats.mode & 0o170000,
  };
}

function sameIdentity(left, right) {
  return left.dev === right.dev &&
    left.ino === right.ino &&
    left.uid === right.uid &&
    left.nlink === right.nlink &&
    left.type === right.type;
}

function directoryIdentity(stats) {
  return {
    dev: stats.dev,
    ino: stats.ino,
    uid: stats.uid,
    type: stats.mode & 0o170000,
  };
}

function sameDirectoryIdentity(left, right) {
  return left.dev === right.dev &&
    left.ino === right.ino &&
    left.uid === right.uid &&
    left.type === right.type;
}

function validateDirectoryStat(stats, label) {
  if (stats.isSymbolicLink() || !stats.isDirectory()) {
    fail(`${label} must be a real directory`);
  }
  if (stats.uid !== currentUid()) {
    fail(`${label} must be owned by the current user`);
  }
  return stats;
}

function directoryTrace(policy, directory) {
  const relative = path.relative(policy.workspace, directory);
  if (path.isAbsolute(relative) || relative === ".." || relative.startsWith(`..${path.sep}`)) {
    fail("parent directory is outside the workspace");
  }
  const locations = [policy.workspace];
  if (relative !== "") {
    let current = policy.workspace;
    for (const component of relative.split(path.sep)) {
      current = path.join(current, component);
      locations.push(current);
    }
  }
  return locations.map((location) => {
    let stats;
    try {
      stats = fs.lstatSync(location);
    } catch (error) {
      fail(`parent directory is unavailable: ${error?.message ?? error}`);
    }
    validateDirectoryStat(stats, "parent directory");
    return { path: location, identity: directoryIdentity(stats) };
  });
}

function assertDirectoryTrace(trace, label) {
  for (const item of trace) {
    let stats;
    try {
      stats = fs.lstatSync(item.path);
    } catch (error) {
      fail(`${label} changed: ${error?.message ?? error}`);
    }
    validateDirectoryStat(stats, label);
    if (!sameDirectoryIdentity(item.identity, directoryIdentity(stats))) {
      fail(`${label} identity changed`);
    }
  }
}

function mutationMode(stats, label) {
  const mode = stats.mode & 0o7777;
  if ((mode & 0o7000) !== 0) {
    fail(`${label} has unsupported special mode bits`);
  }
  return mode & 0o777;
}

function validateRegularStat(stats, label, { maxBytes = MAX_REGULAR_FILE_BYTES } = {}) {
  if (!stats.isFile()) fail(`${label} must be a regular file`);
  if (stats.uid !== currentUid()) fail(`${label} must be owned by the current user`);
  if (stats.nlink !== 1) fail(`${label} must not be a hardlink`);
  if (stats.size > maxBytes) {
    fail(`${label} exceeds the 10 MB regular-file limit`);
  }
  return stats;
}

function lstatTarget(filePath, { allowMissing = false } = {}) {
  try {
    const stats = fs.lstatSync(filePath);
    if (stats.isSymbolicLink()) fail("symlink path is denied");
    return stats;
  } catch (error) {
    if (allowMissing && error?.code === "ENOENT") return undefined;
    if (error instanceof Error && error.message.startsWith("scoped file tools:")) {
      throw error;
    }
    fail(`target is unavailable: ${error?.message ?? error}`);
  }
}

function readFdBytes(fd, label) {
  const initial = validateRegularStat(fs.fstatSync(fd), label);
  const chunks = [];
  let total = 0;
  while (true) {
    const remaining = MAX_REGULAR_FILE_BYTES - total + 1;
    const buffer = Buffer.allocUnsafe(Math.min(IO_CHUNK_BYTES, remaining));
    let count;
    try {
      count = fs.readSync(fd, buffer, 0, buffer.length, null);
    } catch (error) {
      fail(`${label} could not be read: ${error?.message ?? error}`);
    }
    if (count === 0) break;
    total += count;
    if (total > MAX_REGULAR_FILE_BYTES) {
      fail(`${label} exceeds the 10 MB regular-file limit`);
    }
    chunks.push(buffer.subarray(0, count));
  }
  if (initial.size > MAX_REGULAR_FILE_BYTES || total > MAX_REGULAR_FILE_BYTES) {
    fail(`${label} exceeds the 10 MB regular-file limit`);
  }
  return { bytes: Buffer.concat(chunks, total), identity: statIdentity(initial) };
}

function decodeUtf8(bytes, label) {
  try {
    return textDecoder.decode(bytes);
  } catch (error) {
    fail(`${label} is not valid UTF-8: ${error?.message ?? error}`);
  }
}

function encodeUtf8(text, field) {
  assertTextString(text, field);
  const bytes = Buffer.from(text, "utf8");
  if (bytes.length > MAX_REGULAR_FILE_BYTES) {
    fail(`${field} exceeds the 10 MB regular-file limit`);
  }
  return bytes;
}

function writeAll(fd, bytes, label) {
  let written = 0;
  while (written < bytes.length) {
    let count;
    try {
      count = fs.writeSync(fd, bytes, written, bytes.length - written, written);
    } catch (error) {
      fail(`${label} could not be written: ${error?.message ?? error}`);
    }
    if (count <= 0) fail(`${label} write made no progress`);
    written += count;
  }
}

function prepareMutation(policy, toolName, filePath, { allowCreate = toolName === "Write" } = {}) {
  requireDecision(policy, toolName, filePath);
  const target = inspect(policy, filePath, {
    allowDirectory: false,
    allowWorkspaceRoot: false,
    enforceScope: true,
  });
  const parent = path.dirname(target.path);
  const parentTrace = directoryTrace(policy, parent);
  const before = lstatTarget(target.path, { allowMissing: true });
  let mode = 0o644;
  if (before !== undefined) {
    validateRegularStat(before, "target");
    mode = mutationMode(before, "target");
  }
  if (before === undefined && !allowCreate) {
    fail("edit_text target must already exist");
  }
  assertDirectoryTrace(parentTrace, "parent directory");
  return {
    policy,
    toolName,
    filePath,
    target: target.path,
    parent,
    parentTrace,
    before,
    mode,
  };
}

function openExistingForRead(prepared) {
  let fd;
  try {
    fd = fs.openSync(
      prepared.target,
      fs.constants.O_RDONLY | noFollowFlag() | nonBlockFlag(),
    );
    const opened = validateRegularStat(fs.fstatSync(fd), "opened target");
    if (!prepared.before || !sameIdentity(statIdentity(prepared.before), statIdentity(opened))) {
      fail("path identity changed while opening target");
    }
    assertDirectoryTrace(prepared.parentTrace, "parent directory");
    const read = readFdBytes(fd, "target");
    const after = validateRegularStat(fs.lstatSync(prepared.target), "path after read");
    if (!sameIdentity(statIdentity(prepared.before), statIdentity(after)) ||
        !sameIdentity(statIdentity(opened), read.identity)) {
      fail("path identity changed while reading");
    }
    assertDirectoryTrace(prepared.parentTrace, "parent directory");
    return read;
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("scoped file tools:")) {
      throw error;
    }
    fail(`target could not be read: ${error?.message ?? error}`);
  } finally {
    if (fd !== undefined) closeFd(fd);
  }
}

function temporaryPath(parent) {
  temporaryCounter += 1;
  const suffix = `${process.pid}-${Date.now().toString(36)}-${temporaryCounter}-${randomBytes(8).toString("hex")}`;
  return path.join(parent, `.agent-team-${suffix}.tmp`);
}

function createTemporaryFile(parent) {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const candidate = temporaryPath(parent);
    try {
      const fd = fs.openSync(candidate, fs.constants.O_WRONLY | fs.constants.O_CREAT |
        fs.constants.O_EXCL | noFollowFlag(), 0o600);
      return { fd, path: candidate };
    } catch (error) {
      if (error?.code === "EEXIST") continue;
      fail(`temporary file could not be created: ${error?.message ?? error}`);
    }
  }
  fail("could not allocate a unique temporary file");
}

function cleanupTemporaryFile(filePath, identity) {
  if (!filePath) return undefined;
  let stats;
  try {
    stats = fs.lstatSync(filePath);
  } catch (error) {
    if (error?.code === "ENOENT") return undefined;
    return new Error(`could not inspect temporary file: ${error?.message ?? error}`);
  }
  if (stats.isSymbolicLink()) return new Error("temporary identity is unavailable");
  if (!identity) return new Error("temporary identity is unavailable");
  if (!sameIdentity(identity, statIdentity(stats))) {
    return new Error("temporary identity changed; refusing to unlink");
  }
  try {
    fs.unlinkSync(filePath);
  } catch (error) {
    return new Error(`unlink failed: ${error?.message ?? error}`);
  }
  try {
    fs.lstatSync(filePath);
    return new Error("temporary file still exists after unlink");
  } catch (error) {
    if (error?.code === "ENOENT") return undefined;
    return new Error(`could not verify temporary cleanup: ${error?.message ?? error}`);
  }
}

function targetBeforeRename(prepared) {
  const current = lstatTarget(prepared.target, { allowMissing: true });
  if (prepared.before === undefined) {
    if (current !== undefined) fail("target appeared before atomic rename");
    return;
  }
  if (current === undefined) fail("target disappeared before atomic rename");
  validateRegularStat(current, "target before rename");
  if (!sameIdentity(statIdentity(prepared.before), statIdentity(current))) {
    fail("target identity changed before atomic rename");
  }
  if (mutationMode(current, "target before rename") !== prepared.mode) {
    fail("target mode changed before atomic rename");
  }
}

function atomicReplace(prepared, bytes, label) {
  let temporary;
  let temporaryIdentity;
  let renamed = false;
  let result;
  let primaryError;
  try {
    requireDecision(prepared.policy, prepared.toolName, prepared.filePath);
    inspect(prepared.policy, prepared.filePath, {
      allowDirectory: false,
      allowWorkspaceRoot: false,
      enforceScope: true,
    });
    assertDirectoryTrace(prepared.parentTrace, "parent directory");
    targetBeforeRename(prepared);

    temporary = createTemporaryFile(prepared.parent);
    const created = validateRegularStat(fs.fstatSync(temporary.fd), "temporary file");
    temporaryIdentity = statIdentity(created);
    const initial = validateRegularStat(fs.fstatSync(temporary.fd), "temporary file");
    if (initial.size !== 0 || (initial.mode & 0o777) !== 0o600 ||
        (initial.mode & 0o7000) !== 0) {
      fail("temporary file identity or mode is invalid");
    }
    writeAll(temporary.fd, bytes, label);
    fs.fsyncSync(temporary.fd);
    const readyBeforeMode = validateRegularStat(
      fs.fstatSync(temporary.fd),
      "temporary file",
    );
    if (readyBeforeMode.size !== bytes.length ||
        (readyBeforeMode.mode & 0o777) !== 0o600 ||
        (readyBeforeMode.mode & 0o7000) !== 0) {
      fail("temporary file changed before mode application");
    }
    fs.fchmodSync(temporary.fd, prepared.mode);
    const ready = validateRegularStat(fs.fstatSync(temporary.fd), "temporary file");
    if (ready.size !== bytes.length || (ready.mode & 0o777) !== prepared.mode ||
        (ready.mode & 0o7000) !== 0) {
      fail("temporary file changed before atomic rename");
    }
    fs.fsyncSync(temporary.fd);
    temporaryIdentity = statIdentity(ready);
    assertDirectoryTrace(prepared.parentTrace, "parent directory");
    targetBeforeRename(prepared);
    requireDecision(prepared.policy, prepared.toolName, prepared.filePath);
    inspect(prepared.policy, prepared.filePath, {
      allowDirectory: false,
      allowWorkspaceRoot: false,
      enforceScope: true,
    });
    assertDirectoryTrace(prepared.parentTrace, "parent directory");
    fs.renameSync(temporary.path, prepared.target);
    renamed = true;

    assertDirectoryTrace(prepared.parentTrace, "parent directory after rename");
    const after = validateRegularStat(fs.lstatSync(prepared.target), "target after rename");
    if (!sameIdentity(temporaryIdentity, statIdentity(after)) ||
        (after.mode & 0o777) !== prepared.mode || (after.mode & 0o7000) !== 0) {
      fail("target identity or mode changed after atomic rename");
    }
    try {
      fs.lstatSync(temporary.path);
      fail("temporary file remained after atomic rename");
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    result = { file_path: prepared.target, bytes_written: bytes.length };
  } catch (error) {
    primaryError = error instanceof Error && error.message.startsWith("scoped file tools:")
      ? error
      : new Error(`scoped file tools: ${label} failed: ${error?.message ?? error}`);
  } finally {
    if (temporary?.fd !== undefined) {
      try {
        fs.closeSync(temporary.fd);
      } catch (error) {
        const note = `temporary descriptor close failed: ${error?.message ?? error}`;
        primaryError = primaryError
          ? new Error(`${primaryError.message}; ${note}`)
          : new Error(`scoped file tools: ${note}`);
      }
    }
    if (!renamed && temporary) {
      const cleanupError = cleanupTemporaryFile(temporary.path, temporaryIdentity);
      if (cleanupError) {
        const note = `temporary cleanup unconfirmed at ${temporary.path}: ${cleanupError.message}`;
        primaryError = primaryError
          ? new Error(`${primaryError.message}; ${note}`)
          : new Error(`scoped file tools: ${note}`);
      }
    }
  }
  if (primaryError) throw primaryError;
  return result;
}

function closeFd(fd) {
  try {
    fs.closeSync(fd);
  } catch (error) {
    fail(`file descriptor could not be closed: ${error?.message ?? error}`);
  }
}

function readText(policy, args) {
  assertExactObject(args, ["file_path", "offset", "limit"], "read_text arguments");
  const filePath = assertAbsolutePath(args.file_path, "file_path");
  const offset = assertInteger(args.offset, "offset", 1, Number.MAX_SAFE_INTEGER);
  const limit = assertInteger(args.limit, "limit", 1, READ_LINE_LIMIT);
  requireDecision(policy, "Read", filePath);
  const target = inspect(policy, filePath, {
    allowDirectory: false,
    allowWorkspaceRoot: false,
    enforceScope: false,
  });
  const parentTrace = directoryTrace(policy, path.dirname(target.path));
  const before = lstatTarget(target.path);
  validateRegularStat(before, "target");
  let fd;
  try {
    fd = fs.openSync(
      target.path,
      fs.constants.O_RDONLY | noFollowFlag() | nonBlockFlag(),
    );
    const opened = validateRegularStat(fs.fstatSync(fd), "opened target");
    if (!sameIdentity(statIdentity(before), statIdentity(opened))) {
      fail("path identity changed while opening target");
    }
    assertDirectoryTrace(parentTrace, "parent directory");
    const read = readFdBytes(fd, "target");
    const after = validateRegularStat(fs.lstatSync(target.path), "path after read");
    if (!sameIdentity(statIdentity(before), statIdentity(after)) ||
        !sameIdentity(statIdentity(opened), read.identity)) {
      fail("path identity changed while reading");
    }
    assertDirectoryTrace(parentTrace, "parent directory");
    const text = decodeUtf8(read.bytes, "target");
    const lines = text.length === 0 ? [] : text.split(/\r\n|\n|\r/u);
    if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
    const selected = lines.slice(offset - 1, offset - 1 + limit).join("\n");
    const bounded = boundReadText(selected);
    return {
      file_path: target.path,
      start_line: offset,
      end_line: Math.min(offset + limit - 1, lines.length),
      total_lines: lines.length,
      content: bounded.content,
      truncated: bounded.truncated,
    };
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("scoped file tools:")) {
      throw error;
    }
    fail(`read_text failed: ${error?.message ?? error}`);
  } finally {
    if (fd !== undefined) closeFd(fd);
  }
}

function boundReadText(content) {
  const bytes = Buffer.from(content, "utf8");
  if (bytes.length <= MAX_READ_RESULT_BYTES) {
    return { content, truncated: false };
  }
  let end = MAX_READ_RESULT_BYTES;
  while (end > 0) {
    try {
      return {
        content: textDecoder.decode(bytes.subarray(0, end)),
        truncated: true,
      };
    } catch {
      end -= 1;
    }
  }
  return { content: "", truncated: true };
}

function writeText(policy, args) {
  assertExactObject(args, ["file_path", "content"], "write_text arguments");
  const filePath = assertAbsolutePath(args.file_path, "file_path");
  const bytes = encodeUtf8(args.content, "content");
  const prepared = prepareMutation(policy, "Write", filePath);
  try {
    if (prepared.before !== undefined) {
      // Existing regular files must be strict UTF-8 before the replacement is prepared.
      const existing = openExistingForRead(prepared);
      decodeUtf8(existing.bytes, "target");
    }
    return atomicReplace(prepared, bytes, "write_text");
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("scoped file tools:")) {
      throw error;
    }
    fail(`write_text failed: ${error?.message ?? error}`);
  }
}

function findOccurrences(text, needle) {
  const indexes = [];
  let start = 0;
  while (start <= text.length - needle.length) {
    const index = text.indexOf(needle, start);
    if (index === -1) break;
    indexes.push(index);
    start = index + needle.length;
  }
  return indexes;
}

function replaceAtIndexes(text, needle, replacement, indexes) {
  let result = "";
  let cursor = 0;
  for (const index of indexes) {
    result += text.slice(cursor, index);
    result += replacement;
    cursor = index + needle.length;
  }
  return result + text.slice(cursor);
}

function editText(policy, args) {
  assertExactObject(
    args,
    ["file_path", "old_string", "new_string", "replace_all"],
    "edit_text arguments",
  );
  const filePath = assertAbsolutePath(args.file_path, "file_path");
  const oldString = assertTextString(args.old_string, "old_string", { nonEmpty: true });
  const newString = assertTextString(args.new_string, "new_string");
  const replaceAll = assertBoolean(args.replace_all, "replace_all");
  const prepared = prepareMutation(policy, "Edit", filePath, { allowCreate: false });
  try {
    const existing = openExistingForRead(prepared);
    const text = decodeUtf8(existing.bytes, "target");
    const indexes = findOccurrences(text, oldString);
    if (indexes.length === 0) fail("old_string was not found");
    if (!replaceAll && indexes.length > 1) {
      fail("old_string is ambiguous; set replace_all to true");
    }
    const selectedIndexes = replaceAll ? indexes : indexes.slice(0, 1);
    const replacement = replaceAtIndexes(text, oldString, newString, selectedIndexes);
    const bytes = encodeUtf8(replacement, "edited content");
    const result = atomicReplace(prepared, bytes, "edit_text");
    return {
      ...result,
      replacements: selectedIndexes.length,
    };
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("scoped file tools:")) {
      throw error;
    }
    fail(`edit_text failed: ${error?.message ?? error}`);
  }
}

function compareNames(left, right) {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function listResultBytes(directory, files, truncated, visitedEntries) {
  return Buffer.byteLength(JSON.stringify({
    directory,
    files,
    truncated,
    visited_entries: visitedEntries,
  }), "utf8");
}

function listFiles(policy, args) {
  assertExactObject(args, ["directory", "recursive", "limit"], "list_files arguments");
  const directory = assertAbsolutePath(args.directory, "directory");
  const recursive = assertBoolean(args.recursive, "recursive");
  const limit = assertInteger(args.limit, "limit", 1, LIST_LIMIT);
  const target = inspect(policy, directory, {
    allowDirectory: true,
    allowWorkspaceRoot: true,
    enforceScope: false,
  });
  const rootStats = lstatTarget(target.path);
  if (!rootStats.isDirectory()) fail("directory must be a real directory");

  const pending = [target.path];
  const files = [];
  let visitedEntries = 0;
  let truncated = false;
  let pendingIndex = 0;
  while (pendingIndex < pending.length && !truncated) {
    const current = pending[pendingIndex];
    pendingIndex += 1;
    let currentCheck;
    try {
      currentCheck = inspect(policy, current, {
        allowDirectory: true,
        allowWorkspaceRoot: true,
        enforceScope: false,
      });
    } catch {
      continue;
    }
    const currentStats = lstatTarget(currentCheck.path);
    if (!currentStats.isDirectory()) continue;
    const currentTrace = directoryTrace(policy, currentCheck.path);
    let directoryHandle;
    try {
      directoryHandle = fs.opendirSync(currentCheck.path, { bufferSize: 32 });
      assertDirectoryTrace(currentTrace, "parent directory before traversal");
      while (!truncated) {
        let entry;
        try {
          entry = directoryHandle.readSync();
        } catch (error) {
          fail(`directory could not be read: ${error?.message ?? error}`);
        }
        if (entry === null) break;
        if (visitedEntries >= MAX_VISITED_ENTRIES) {
          truncated = true;
          break;
        }
        visitedEntries += 1;
        const candidate = path.join(currentCheck.path, entry.name);
        let stats;
        try {
          stats = fs.lstatSync(candidate);
        } catch {
          continue;
        }
        if (stats.isSymbolicLink()) continue;
        let childPath = candidate;
        if (entry.name === ".git" || policy.protected_paths.includes(candidate)) {
          const childCheck = scopedFunctionInspect(policy, candidate, stats.isDirectory());
          if (!childCheck.ok) continue;
          childPath = childCheck.path;
        }
        if (stats.isDirectory()) {
          if (recursive) pending.push(childPath);
          continue;
        }
        if (!stats.isFile() || stats.uid !== currentUid() || stats.nlink !== 1 ||
            stats.size > MAX_REGULAR_FILE_BYTES) {
          continue;
        }
        if (files.length >= limit) {
          truncated = true;
          break;
        }
        const candidateFiles = [...files, childPath];
        if (listResultBytes(target.path, candidateFiles, true, visitedEntries) >
            MAX_TOOL_RESULT_BYTES) {
          truncated = true;
          break;
        }
        files.push(childPath);
      }
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("scoped file tools:")) {
        throw error;
      }
      fail(`directory could not be read: ${error?.message ?? error}`);
    } finally {
      if (directoryHandle !== undefined) directoryHandle.closeSync();
    }
    assertDirectoryTrace(currentTrace, "parent directory after traversal");
  }
  files.sort(compareNames);
  const result = {
    directory: target.path,
    files,
    truncated,
    visited_entries: visitedEntries,
  };
  if (listResultBytes(target.path, files, truncated, visitedEntries) > MAX_TOOL_RESULT_BYTES) {
    fail("list_files result exceeds the 1 MiB output limit");
  }
  return result;
}

function scopedFunctionInspect(policy, filePath, allowDirectory) {
  try {
    return inspectTarget(policy, filePath, {
      allowDirectory,
      allowWorkspaceRoot: false,
      enforceScope: false,
    });
  } catch {
    return { ok: false, reason: "shared policy denied the target" };
  }
}

function jsonSafe(value) {
  let encoded;
  try {
    encoded = JSON.stringify(value);
  } catch (error) {
    fail(`result is not JSON serializable: ${error?.message ?? error}`);
  }
  if (encoded === undefined) fail("result is not JSON serializable");
  return JSON.parse(encoded);
}

const DYNAMIC_FILE_TOOLS_MUTABLE = [
  {
    type: "function",
    name: "read_text",
    description: "Read a bounded UTF-8 regular text file by one-based line range.",
    inputSchema: {
      type: "object",
      properties: {
        file_path: { type: "string", minLength: 1, description: "Absolute regular text file path." },
        offset: { type: "integer", minimum: 1, description: "One-based first line." },
        limit: { type: "integer", minimum: 1, maximum: READ_LINE_LIMIT, description: "Maximum lines." },
      },
      required: ["file_path", "offset", "limit"],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "write_text",
    description: "Replace a regular UTF-8 text file within the guarded workspace.",
    inputSchema: {
      type: "object",
      properties: {
        file_path: { type: "string", minLength: 1, description: "Absolute regular text file path." },
        content: { type: "string", description: "Complete replacement text." },
      },
      required: ["file_path", "content"],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "edit_text",
    description: "Replace a non-empty UTF-8 text substring in a guarded regular file.",
    inputSchema: {
      type: "object",
      properties: {
        file_path: { type: "string", minLength: 1, description: "Absolute regular text file path." },
        old_string: { type: "string", minLength: 1, description: "Non-empty text to find." },
        new_string: { type: "string", description: "Replacement text." },
        replace_all: { type: "boolean", description: "Replace every occurrence." },
      },
      required: ["file_path", "old_string", "new_string", "replace_all"],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "list_files",
    description: "List guarded regular files under an absolute directory.",
    inputSchema: {
      type: "object",
      properties: {
        directory: { type: "string", minLength: 1, description: "Absolute directory path." },
        recursive: { type: "boolean", description: "Traverse child directories." },
        limit: { type: "integer", minimum: 1, maximum: LIST_LIMIT, description: "Maximum files." },
      },
      required: ["directory", "recursive", "limit"],
      additionalProperties: false,
    },
  },
];

function deepFreeze(value) {
  if (!isObject(value) && !Array.isArray(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

export const DYNAMIC_FILE_TOOLS = deepFreeze(DYNAMIC_FILE_TOOLS_MUTABLE);

export function executeFileTool(rawPolicy, toolName, args) {
  if (typeof toolName !== "string" || !["read_text", "write_text", "edit_text", "list_files"].includes(toolName)) {
    fail(`unknown file tool: ${String(toolName)}`);
  }
  const policy = normalizedPolicy(rawPolicy);
  let result;
  if (toolName === "read_text") result = readText(policy, args);
  else if (toolName === "write_text") result = writeText(policy, args);
  else if (toolName === "edit_text") result = editText(policy, args);
  else result = listFiles(policy, args);
  return jsonSafe(result);
}
