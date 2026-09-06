#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

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
const FIXED_DISALLOWED_TOOLS = Object.freeze([
  "Bash",
  "Task",
  "Agent",
  "WebFetch",
  "WebSearch",
  "NotebookEdit",
  "AskUserQuestion",
]);
const DENIED_AGENT_METHODS = Object.freeze([
  "authenticate",
  "logout",
  "unstable_listProviders",
  "unstable_setProvider",
  "unstable_disableProvider",
  "listSessions",
  "deleteSession",
  "steer",
  "goal",
]);
const POLICY_VERSION = 1;
const CLAUDE_ACP_PACKAGE = "@agentclientprotocol/claude-agent-acp";
const CLAUDE_ACP_VERSION = "0.70.0";
const DEFAULT_MODE = "default";
const PROTECTED_COMPONENTS = new Set([
  ".git",
]);

function fail(message) {
  throw new Error(`claude scoped ACP: ${message}`);
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

function inspectTarget(policy, rawPath, { allowDirectory = false, enforceScope = true, allowWorkspaceRoot = false } = {}) {
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

export async function hookDecision(rawPolicy, input) {
  const policy = normalizePolicy(rawPolicy);
  const toolName = isObject(input) ? input.tool_name : undefined;
  const toolInput = isObject(input) ? input.tool_input : undefined;
  return decideTool(policy, toolName, toolInput);
}

export function createPreToolUseHook(rawPolicy) {
  const policy = isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : normalizePolicy(rawPolicy);
  return async (input) => {
    if (!isObject(input) || input.hook_event_name !== "PreToolUse") return {};
    const decision = decideTool(policy, input.tool_name, input.tool_input);
    return {
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: decision.behavior,
        permissionDecisionReason: decision.message,
      },
    };
  };
}

function guardSessionMode(params) {
  if (!isObject(params)) fail("session mode params must be an object");
  const mode = params.modeId ?? (params.configId === "mode" ? params.value : undefined);
  if (mode === "bypassPermissions" || mode === "acceptEdits" || mode === "auto") {
    fail(`session mode is not allowed: ${mode}`);
  }
  return params;
}

export function assertAmbientDefaultMode(mode) {
  if (mode !== undefined && mode !== "default" && mode !== "manual") {
    fail(`ambient permission mode is not allowed: ${String(mode)}`);
  }
  return true;
}

function sameStringArray(left, right) {
  return Array.isArray(left) && left.length === right.length && left.every((value, index) => value === right[index]);
}

function securityOptions(rawOptions, policy) {
  if (!isObject(policy) || policy.version !== POLICY_VERSION) {
    policy = normalizePolicy(policy);
  }
  const incoming = isObject(rawOptions) ? rawOptions : {};
  const fixedTools = toolsForPolicy(policy);
  if (incoming.tools !== undefined && !sameStringArray(incoming.tools, fixedTools)) {
    fail("session options cannot override tools");
  }
  if (incoming.allowedTools !== undefined && !sameStringArray(incoming.allowedTools, fixedTools)) {
    fail("session options cannot override allowedTools");
  }
  for (const key of [
    "hooks",
    "settings",
    "mcpServers",
    "additionalDirectories",
    "permissionMode",
    "allowDangerouslySkipPermissions",
    "sandbox",
    "env",
    "persistSession",
  ]) {
    if (incoming[key] !== undefined) fail(`session options cannot override ${key}`);
  }
  const options = {};
  if (typeof incoming.model === "string" && incoming.model.length > 0) options.model = incoming.model;
  if (typeof incoming.systemPrompt === "string" || (isObject(incoming.systemPrompt) && typeof incoming.systemPrompt.append === "string")) {
    options.systemPrompt = incoming.systemPrompt;
  }
  if (typeof incoming.maxTurns === "number" && Number.isInteger(incoming.maxTurns) && incoming.maxTurns > 0) {
    options.maxTurns = incoming.maxTurns;
  }
  options.settingSources = [];
  options.persistSession = false;
  options.settings = {
    autoMemoryEnabled: false,
    permissions: {
      defaultMode: DEFAULT_MODE,
      disableBypassPermissionsMode: "disable",
      allow: fixedTools.map((tool) => `${tool}(*)`),
      deny: [
        "Bash(*)",
        "Task(*)",
        "Agent(*)",
        "WebFetch(*)",
        "WebSearch(*)",
        ...(policy.permission === "read-only" ? ["Write(*)", "Edit(*)"] : []),
      ],
    },
  };
  options.tools = [...fixedTools];
  options.allowedTools = [...fixedTools];
  options.disallowedTools = [
    ...FIXED_DISALLOWED_TOOLS,
    ...(policy.permission === "read-only" ? [...WRITE_TOOLS] : []),
  ];
  options.mcpServers = {};
  options.additionalDirectories = [];
  options.hooks = {
    PreToolUse: [{ hooks: [createPreToolUseHook(policy)] }],
  };
  return options;
}

export function fixedSessionOptions(rawPolicy, rawOptions = undefined) {
  return securityOptions(rawOptions, normalizePolicy(rawPolicy));
}

function safeMeta(rawMeta, policy) {
  const meta = isObject(rawMeta) ? { ...rawMeta } : {};
  const claudeCode = isObject(meta.claudeCode) ? { ...meta.claudeCode } : {};
  claudeCode.options = securityOptions(claudeCode.options, policy);
  meta.claudeCode = claudeCode;
  delete meta.additionalRoots;
  return meta;
}

export function injectSessionParams(rawParams, rawPolicy) {
  if (!isObject(rawParams)) fail("session params must be an object");
  const policy = isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : normalizePolicy(rawPolicy);
  if (rawParams.cwd !== policy.workspace) fail("session cwd does not match policy workspace");
  if (rawParams.mcpServers !== undefined &&
      (!Array.isArray(rawParams.mcpServers) || rawParams.mcpServers.length > 0)) {
    fail("MCP servers are not allowed");
  }
  if (rawParams.additionalDirectories !== undefined &&
      (!Array.isArray(rawParams.additionalDirectories) || rawParams.additionalDirectories.length > 0)) {
    fail("additional directories are not allowed");
  }
  return {
    ...rawParams,
    cwd: policy.workspace,
    mcpServers: [],
    additionalDirectories: [],
    _meta: safeMeta(rawParams._meta, policy),
  };
}

function packageRootFromEntry(entry) {
  const raw = assertString(entry, "agent entry");
  if (!path.isAbsolute(raw)) fail("agent entry must be absolute");
  const resolved = fs.realpathSync.native(path.resolve(raw));
  if (path.basename(resolved) !== "index.js" || path.basename(path.dirname(resolved)) !== "dist") {
    fail("agent entry must resolve to dist/index.js");
  }
  const packageRoot = path.dirname(path.dirname(resolved));
  const packageFile = path.join(packageRoot, "package.json");
  const packageInfo = JSON.parse(fs.readFileSync(packageFile, "utf8"));
  if (packageInfo.name !== CLAUDE_ACP_PACKAGE || packageInfo.version !== CLAUDE_ACP_VERSION) {
    fail("agent entry package identity does not match Claude ACP 0.70.0");
  }
  const libPath = path.join(path.dirname(resolved), "lib.js");
  const relativeLibrary = path.relative(packageRoot, libPath);
  if (
    path.isAbsolute(relativeLibrary) ||
    relativeLibrary === ".." ||
    relativeLibrary.startsWith(`..${path.sep}`)
  ) {
    fail("agent entry sibling lib.js is outside its package");
  }
  let libraryInfo;
  try {
    libraryInfo = fs.lstatSync(libPath);
  } catch (error) {
    fail(`agent entry sibling lib.js is unavailable: ${error?.message ?? error}`);
  }
  if (libraryInfo.isSymbolicLink()) {
    fail("agent entry sibling lib.js must not be a symlink");
  }
  if (!libraryInfo.isFile()) fail("agent entry sibling lib.js must be a regular file");
  const canonicalLibrary = fs.realpathSync.native(libPath);
  if (canonicalLibrary !== libPath || !within(canonicalLibrary, packageRoot)) {
    fail("agent entry sibling lib.js is outside its package");
  }
  return { entry: resolved, packageRoot, libPath: canonicalLibrary };
}

export function resolveAgentLibrary(entry) {
  return pathToFileURL(packageRootFromEntry(entry).libPath).href;
}

function wrapSessionMethod(agent, method, policy) {
  const original = agent[method];
  if (typeof original !== "function") fail(`agent does not expose ${method}`);
  agent[method] = function scopedSessionMethod(params, ...rest) {
    return original.call(agent, injectSessionParams(params, policy), ...rest);
  };
}

export function installSessionGuards(agent, rawPolicy) {
  const policy = isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : normalizePolicy(rawPolicy);
  for (const method of ["newSession", "loadSession", "resumeSession", "unstable_forkSession"]) {
    wrapSessionMethod(agent, method, policy);
  }
  for (const method of ["setSessionMode", "setSessionConfigOption"]) {
    const original = agent[method];
    if (typeof original !== "function") fail(`agent does not expose ${method}`);
    agent[method] = function scopedModeMethod(params, ...rest) {
      return original.call(agent, guardSessionMode(params), ...rest);
    };
  }
  for (const method of DENIED_AGENT_METHODS) {
    const original = agent[method];
    if (typeof original !== "function") fail(`agent does not expose ${method}`);
    agent[method] = function deniedAgentMethod() {
      fail(`ACP method is not allowed: ${method}`);
    };
  }
  return agent;
}

async function runMain() {
  const args = process.argv.slice(2);
  if (args.length !== 4 || args[0] !== "--agent-entry" || args[2] !== "--policy") {
    fail("usage: --agent-entry /absolute/dist/index.js --policy /absolute/write-policy.json");
  }
  const entry = args[1];
  const policyPath = args[3];
  const policy = await loadPolicy(policyPath);
  const entryInfo = packageRootFromEntry(entry);
  if (!policy.protected_paths.includes(entryInfo.packageRoot)) {
    fail("policy protected_paths must include the installed agent package root");
  }
  const library = await import(pathToFileURL(entryInfo.libPath).href);
  if (typeof library.runAcp !== "function") fail("installed ACP library does not export runAcp");
  if (typeof library.SettingsManager !== "function") fail("installed ACP library does not expose SettingsManager");
  const settingsManager = new library.SettingsManager(policy.workspace);
  try {
    await settingsManager.initialize();
    assertAmbientDefaultMode(settingsManager.getSettings()?.permissions?.defaultMode);
  } finally {
    settingsManager.dispose();
  }
  console.log = console.error;
  console.info = console.error;
  console.warn = console.error;
  console.debug = console.error;
  const { connection, agent } = library.runAcp(undefined);
  installSessionGuards(agent, policy);
  let shuttingDown = false;
  const shutdown = async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    try {
      await agent.dispose();
    } catch (error) {
      process.stderr.write(`scoped ACP cleanup failed: ${error?.message ?? error}\n`);
      process.exitCode = 1;
    }
    process.exit(process.exitCode ?? 0);
  };
  connection.closed.then(shutdown, shutdown);
  process.once("SIGTERM", shutdown);
  process.once("SIGINT", shutdown);
  process.stdin.resume();
}

if (
  process.argv[1] &&
  fs.realpathSync.native(process.argv[1]) === fileURLToPath(import.meta.url) &&
  process.argv[2] === "--agent-entry"
) {
  runMain().catch((error) => {
    process.stderr.write(`${error?.message ?? error}\n`);
    process.exitCode = 1;
  });
}
