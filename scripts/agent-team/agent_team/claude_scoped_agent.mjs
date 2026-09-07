#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { decideTool, loadPolicy, parsePolicy } from "./scoped_policy.mjs";

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

function policyForUse(rawPolicy) {
  return isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : parsePolicy(rawPolicy);
}

function toolsForPolicy(policy) {
  return policy.permission === "read-only" ? READ_ONLY_TOOLS : WORKSPACE_WRITE_TOOLS;
}

function within(candidate, root) {
  return candidate === root || !path.isAbsolute(path.relative(root, candidate)) &&
    !path.relative(root, candidate).startsWith(`..${path.sep}`) &&
    path.relative(root, candidate) !== "..";
}

export async function hookDecision(rawPolicy, input) {
  const policy = parsePolicy(rawPolicy);
  const toolName = isObject(input) ? input.tool_name : undefined;
  const toolInput = isObject(input) ? input.tool_input : undefined;
  return decideTool(policy, toolName, toolInput);
}

export function createPreToolUseHook(rawPolicy) {
  const policy = policyForUse(rawPolicy);
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
    policy = policyForUse(policy);
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
  return securityOptions(rawOptions, policyForUse(rawPolicy));
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
    : policyForUse(rawPolicy);
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
    : policyForUse(rawPolicy);
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
