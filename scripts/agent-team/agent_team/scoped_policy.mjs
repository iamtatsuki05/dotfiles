#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

const POLICY_KEYS = new Set([
  "workspace",
  "allowed_paths",
  "forbidden_paths",
  "protected_paths",
  "permission",
]);
const READ_ONLY_TOOLS = Object.freeze(["Read", "Grep", "Glob"]);
const WORKSPACE_WRITE_TOOLS = Object.freeze(["Read", "Grep", "Glob", "Write", "Edit"]);
const WRITE_TOOLS = new Set(["Write", "Edit"]);
const POLICY_VERSION = 1;
const PROTECTED_COMPONENTS = new Set([
  ".git",
]);

function fail(message) {
  throw new Error(`scoped ACP: ${message}`);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function assertString(value, field) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    fail(`${field} must be a non-empty string without NUL`);
  }
  if ([...value].some((character) => {
    const code = character.codePointAt(0);
    return code !== undefined && code < 0x20;
  })) {
    fail(`${field} must not contain control characters`);
  }
  return value;
}

function assertArray(value, field) {
  if (!Array.isArray(value)) fail(`${field} must be an array`);
  return value;
}

function canonicalExistingDirectory(raw, field) {
  const value = assertString(raw, field);
  if (!path.isAbsolute(value)) fail(`${field} must be absolute`);
  const absolute = path.resolve(value);
  let info;
  try {
    info = fs.lstatSync(absolute);
  } catch (error) {
    fail(`${field} is unavailable: ${error?.message ?? error}`);
  }
  if (!info.isDirectory() || info.isSymbolicLink()) {
    fail(`${field} must be a real directory`);
  }
  const canonical = fs.realpathSync.native(absolute);
  if (canonical !== absolute) fail(`${field} must be its canonical real path`);
  return canonical;
}

function canonicalExistingPath(raw, field) {
  const value = assertString(raw, field);
  if (!path.isAbsolute(value)) fail(`${field} must be absolute`);
  const absolute = path.resolve(value);
  let info;
  try {
    info = fs.lstatSync(absolute);
  } catch (error) {
    fail(`${field} is unavailable: ${error?.message ?? error}`);
  }
  if (info.isSymbolicLink()) fail(`${field} must not be a symlink`);
  const canonical = fs.realpathSync.native(absolute);
  if (canonical !== absolute) fail(`${field} must be its canonical real path`);
  return canonical;
}

function relativeRule(raw, field) {
  const value = assertString(raw, field);
  if (value.includes("\\") || value.startsWith("/") || value.startsWith("~")) {
    fail(`${field} must be a workspace-relative POSIX path`);
  }
  const body = value.endsWith("/") ? value.slice(0, -1) : value;
  const parts = body.split("/");
  if (!body || parts.some((part) => part === "" || part === "." || part === "..")) {
    fail(`${field} must not escape or ambiguously identify the workspace`);
  }
  if ([...value].some((character) => {
    const code = character.codePointAt(0);
    return code !== undefined && (code < 0x20 || code === 0x7f);
  })) {
    fail(`${field} must not contain control characters`);
  }
  return value;
}

function unique(values, field) {
  if (new Set(values).size !== values.length) fail(`${field} must not contain duplicates`);
  return values;
}

function toolsForPolicy(policy) {
  return policy.permission === "read-only" ? READ_ONLY_TOOLS : WORKSPACE_WRITE_TOOLS;
}

function normalizePolicy(raw) {
  if (!isObject(raw)) fail("policy must be an object");
  const keys = Object.keys(raw);
  if (keys.length !== POLICY_KEYS.size || keys.some((key) => !POLICY_KEYS.has(key))) {
    fail("policy fields must be exactly workspace, allowed_paths, forbidden_paths, protected_paths, permission");
  }
  const permission = assertString(raw.permission, "permission");
  if (permission !== "read-only" && permission !== "workspace-write") {
    fail("permission must be read-only or workspace-write");
  }
  const workspace = canonicalExistingDirectory(raw.workspace, "workspace");
  const allowedPaths = unique(
    assertArray(raw.allowed_paths, "allowed_paths").map((value, index) =>
      relativeRule(value, `allowed_paths[${index}]`),
    ),
    "allowed_paths",
  );
  const forbiddenPaths = unique(
    assertArray(raw.forbidden_paths, "forbidden_paths").map((value, index) =>
      relativeRule(value, `forbidden_paths[${index}]`),
    ),
    "forbidden_paths",
  );
  const protectedPaths = unique(
    assertArray(raw.protected_paths, "protected_paths").map((value, index) =>
      canonicalExistingPath(value, `protected_paths[${index}]`),
    ),
    "protected_paths",
  );
  if (protectedPaths.length === 0) fail("protected_paths must not be empty");
  return {
    version: POLICY_VERSION,
    workspace,
    allowed_paths: allowedPaths,
    forbidden_paths: forbiddenPaths,
    protected_paths: protectedPaths,
    permission,
  };
}

export function parsePolicy(raw) {
  return normalizePolicy(raw);
}

export async function loadPolicy(policyPath) {
  const value = assertString(policyPath, "policy path");
  if (!path.isAbsolute(value)) fail("policy path must be absolute");
  const absolute = path.resolve(value);
  let info;
  try {
    info = await fsp.lstat(absolute);
  } catch (error) {
    fail(`policy file is unavailable: ${error?.message ?? error}`);
  }
  if (!info.isFile() || info.isSymbolicLink()) fail("policy file must be a regular non-symlink file");
  if (info.uid !== process.getuid?.() || (info.mode & 0o777) !== 0o600) {
    fail("policy file must be owned by the current user and mode 0600");
  }
  let parsed;
  try {
    parsed = JSON.parse(await fsp.readFile(absolute, "utf8"));
  } catch (error) {
    fail(`policy JSON is invalid: ${error?.message ?? error}`);
  }
  return normalizePolicy(parsed);
}

function relativeToWorkspace(policy, candidate) {
  const relative = path.relative(policy.workspace, candidate);
  if (path.isAbsolute(relative) || relative.startsWith(`..${path.sep}`) || relative === "..") {
    return undefined;
  }
  return relative.split(path.sep).join("/");
}

function within(candidate, root) {
  return candidate === root || !path.isAbsolute(path.relative(root, candidate)) &&
    !path.relative(root, candidate).startsWith(`..${path.sep}`) &&
    path.relative(root, candidate) !== "..";
}

function ruleMatches(relative, rule) {
  const directory = rule.endsWith("/");
  const body = directory ? rule.slice(0, -1) : rule;
  return directory ? relative === body || relative.startsWith(`${body}/`) : relative === body;
}

function isProtectedName(part) {
  return PROTECTED_COMPONENTS.has(part);
}

function caseExactEntry(parent, name) {
  let entries;
  try {
    entries = fs.readdirSync(parent, { withFileTypes: true });
  } catch (error) {
    return { ok: false, reason: `parent is unavailable: ${error?.message ?? error}` };
  }
  const folded = name.toLocaleLowerCase();
  const matches = entries.filter((entry) => entry.name.toLocaleLowerCase() === folded);
  if (matches.length > 1 || (matches.length === 1 && matches[0].name !== name)) {
    return { ok: false, reason: "path name has a case ambiguity" };
  }
  return { ok: true, entry: matches[0] };
}

export function inspectTarget(policy, rawPath, { allowDirectory = false, enforceScope = true, allowWorkspaceRoot = false } = {}) {
  if (typeof rawPath !== "string" || !path.isAbsolute(rawPath) || rawPath.includes("\0")) {
    return { ok: false, reason: "path must be an absolute path without NUL" };
  }
  const rawParts = rawPath.split(path.sep);
  if (rawParts.includes("..")) return { ok: false, reason: "path traversal is denied" };
  const candidate = path.resolve(rawPath);
  if (!within(candidate, policy.workspace) || (candidate === policy.workspace && !allowWorkspaceRoot)) {
    return { ok: false, reason: "path is outside the workspace" };
  }
  if (policy.protected_paths.some((root) => within(candidate, root) || within(root, candidate))) {
    return { ok: false, reason: "path is protected" };
  }
  const relative = relativeToWorkspace(policy, candidate);
  if (relative === undefined) return { ok: false, reason: "path is outside the workspace" };
  const components = relative.split("/");
  if (components.some(isProtectedName)) return { ok: false, reason: "protected config/auth/state path" };
  if (enforceScope) {
    const denied = policy.forbidden_paths.some((rule) => ruleMatches(relative, rule));
    if (denied) return { ok: false, reason: "path is forbidden by policy" };
    const allowed = policy.allowed_paths.some((rule) => ruleMatches(relative, rule));
    if (!allowed) return { ok: false, reason: "path is outside allowed_paths" };
  }

  let current = policy.workspace;
  for (let index = 0; index < components.length; index += 1) {
    const component = components[index];
    const caseCheck = caseExactEntry(current, component);
    if (!caseCheck.ok) return caseCheck;
    const next = path.join(current, component);
    let info;
    try {
      info = fs.lstatSync(next);
    } catch (error) {
      if (error?.code === "ENOENT" && index === components.length - 1) {
        current = next;
        continue;
      }
      return { ok: false, reason: `path is unavailable: ${error?.message ?? error}` };
    }
    if (info.isSymbolicLink()) return { ok: false, reason: "symlink path is denied" };
    if (index < components.length - 1) {
      if (!info.isDirectory()) return { ok: false, reason: "parent is not a directory" };
    } else if (info.isDirectory()) {
      if (!allowDirectory) return { ok: false, reason: "directory is not a file target" };
    } else if (!info.isFile()) {
      return { ok: false, reason: "special file is denied" };
    } else if (info.nlink !== 1) {
      return { ok: false, reason: "hardlink file is denied" };
    }
    current = next;
  }
  return { ok: true, path: candidate, relative };
}

function pathFromInput(toolName, input) {
  if (!isObject(input)) return undefined;
  if (typeof input.file_path === "string") return input.file_path;
  if (toolName === "Read" && typeof input.path === "string") return input.path;
  if ((toolName === "Grep" || toolName === "Glob") && typeof input.path === "string") {
    return input.path;
  }
  return undefined;
}

function globHasMagic(segment) {
  return /[*?\[\]{}]/u.test(segment);
}

function protectedSubtreeExists(policy, root) {
  if (policy.protected_paths.some((protectedPath) => within(protectedPath, root))) return true;
  const pending = [root];
  let visited = 0;
  while (pending.length > 0) {
    const current = pending.pop();
    if (!current || ++visited > 10_000) return true;
    let entries;
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      return true;
    }
    for (const entry of entries) {
      if (entry.name === ".git") return true;
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
      pending.push(path.join(current, entry.name));
    }
  }
  return false;
}

function decideGlob(policy, input) {
  if (!isObject(input) || typeof input.pattern !== "string" || input.pattern.length === 0) {
    return { behavior: "deny", message: "Glob requires a relative pattern" };
  }
  const pattern = input.pattern;
  if (path.isAbsolute(pattern) || pattern.includes("\\") || pattern.split("/").includes("..")) {
    return { behavior: "deny", message: "Glob pattern must stay relative to the workspace" };
  }
  const rawBase = input.path === undefined ? policy.workspace : input.path;
  if (typeof rawBase !== "string") return { behavior: "deny", message: "Glob path is invalid" };
  const base = path.isAbsolute(rawBase) ? rawBase : path.resolve(policy.workspace, rawBase);
  const baseCheck = inspectTarget(policy, base, {
    allowDirectory: true,
    allowWorkspaceRoot: true,
    enforceScope: false,
  });
  if (!baseCheck.ok) return { behavior: "deny", message: baseCheck.reason };
  const segments = pattern.split("/");
  const staticSegments = [];
  for (const segment of segments) {
    if (globHasMagic(segment)) break;
    staticSegments.push(segment);
  }
  const searchRoot = path.join(baseCheck.path, ...staticSegments);
  const rootCheck = inspectTarget(policy, searchRoot, {
    allowDirectory: true,
    allowWorkspaceRoot: true,
    enforceScope: false,
  });
  if (!rootCheck.ok) return { behavior: "deny", message: rootCheck.reason };
  if (protectedSubtreeExists(policy, rootCheck.path) && pattern.includes("**")) {
    return { behavior: "deny", message: "Glob recursion could enter a protected subtree" };
  }
  return { behavior: "allow", message: "relative Glob search is confined to the workspace" };
}

export function decideTool(rawPolicy, toolName, input) {
  const policy = isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : normalizePolicy(rawPolicy);
  if (!toolsForPolicy(policy).includes(toolName)) {
    return { behavior: "deny", message: `tool is not allowed: ${String(toolName)}` };
  }
  if (toolName === "Glob") return decideGlob(policy, input);
  const rawPath = pathFromInput(toolName, input);
  if (rawPath === undefined) {
    if (toolName === "Grep" || toolName === "Glob") {
      return { behavior: "allow", message: `${toolName} uses the session workspace as its read root` };
    }
    return { behavior: "deny", message: `${toolName} requires an explicit path` };
  }
  const writeTool = toolName === "Write" || toolName === "Edit";
  const target = inspectTarget(policy, rawPath, {
    allowDirectory: toolName === "Grep" || toolName === "Glob",
    enforceScope: writeTool,
  });
  if (!target.ok) return { behavior: "deny", message: target.reason };
  return { behavior: "allow", message: `allowed ${toolName} for ${target.relative}` };
}
