#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { decideTool, loadPolicy, parsePolicy } from "./scoped_policy.mjs";

const READ_ONLY_TOOLS = Object.freeze(["Read", "Grep", "Glob"]);
const WORKSPACE_WRITE_TOOLS = Object.freeze(["Read", "Grep", "Glob", "Write", "Edit"]);
const ASK_USER_TOOL = "AskUserQuestion";
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

function toolsForPolicy(policy, questionsEnabled = false) {
  const tools = policy.permission === "read-only" ? READ_ONLY_TOOLS : WORKSPACE_WRITE_TOOLS;
  return questionsEnabled ? [...tools, ASK_USER_TOOL] : [...tools];
}

function within(candidate, root) {
  return candidate === root || !path.isAbsolute(path.relative(root, candidate)) &&
    !path.relative(root, candidate).startsWith(`..${path.sep}`) &&
    path.relative(root, candidate) !== "..";
}

function questionText(value, field) {
  if (typeof value !== "string" || value.length === 0 || value.includes("\0")) {
    fail(`${field} must be a non-empty string without NUL`);
  }
  if ([...value].length > 20_000) fail(`${field} exceeds 20000 characters`);
  return value;
}

function validateAskUserQuestionInput(toolInput) {
  if (!isObject(toolInput)) fail("AskUserQuestion input must be an object");
  const keys = Object.keys(toolInput);
  if (keys.length !== 1 || keys[0] !== "questions") {
    fail("AskUserQuestion input must contain questions and no prefilled answers");
  }
  if (!Array.isArray(toolInput.questions) || toolInput.questions.length === 0 || toolInput.questions.length > 4) {
    fail("AskUserQuestion questions must contain one to four entries");
  }
  for (const [index, question] of toolInput.questions.entries()) {
    if (!isObject(question)) fail(`AskUserQuestion questions[${index}] must be an object`);
    const allowed = new Set(["question", "header", "options", "multiSelect"]);
    if (Object.keys(question).some((key) => !allowed.has(key))) {
      fail(`AskUserQuestion questions[${index}] has an unknown field`);
    }
    questionText(question.question, `AskUserQuestion questions[${index}].question`);
    if (question.header !== undefined) questionText(question.header, `AskUserQuestion questions[${index}].header`);
    if (question.multiSelect !== undefined && typeof question.multiSelect !== "boolean") {
      fail(`AskUserQuestion questions[${index}].multiSelect must be boolean`);
    }
    if (!Array.isArray(question.options) || question.options.length === 0) {
      fail(`AskUserQuestion questions[${index}].options must be non-empty`);
    }
    for (const [optionIndex, option] of question.options.entries()) {
      if (!isObject(option)) fail(`AskUserQuestion questions[${index}].options[${optionIndex}] must be an object`);
      const optionKeys = new Set(["label", "description", "preview"]);
      if (Object.keys(option).some((key) => !optionKeys.has(key))) {
        fail(`AskUserQuestion questions[${index}].options[${optionIndex}] has an unknown field`);
      }
      questionText(option.label, `AskUserQuestion questions[${index}].options[${optionIndex}].label`);
      if (option.description !== undefined) {
        questionText(option.description, `AskUserQuestion questions[${index}].options[${optionIndex}].description`);
      }
      if (option.preview !== undefined) {
        questionText(option.preview, `AskUserQuestion questions[${index}].options[${optionIndex}].preview`);
      }
    }
  }
  return true;
}

function askToolDecision(toolInput, questionsEnabled) {
  if (!questionsEnabled) {
    return { behavior: "deny", message: "AskUserQuestion is disabled without a question channel" };
  }
  try {
    validateAskUserQuestionInput(toolInput);
  } catch (error) {
    return { behavior: "deny", message: error?.message ?? String(error) };
  }
  return {
    behavior: "ask",
    message: "AskUserQuestion requires ACP form elicitation",
  };
}

export async function hookDecision(rawPolicy, input, rawOptions = undefined) {
  const policy = parsePolicy(rawPolicy);
  const toolName = isObject(input) ? input.tool_name : undefined;
  const toolInput = isObject(input) ? input.tool_input : undefined;
  if (toolName === ASK_USER_TOOL) {
    return askToolDecision(toolInput, rawOptions?.questionsEnabled === true);
  }
  return decideTool(policy, toolName, toolInput);
}

export function createPreToolUseHook(rawPolicy, rawOptions = undefined) {
  const policy = policyForUse(rawPolicy);
  const questionsEnabled = rawOptions?.questionsEnabled === true;
  return async (input) => {
    if (!isObject(input) || input.hook_event_name !== "PreToolUse") return {};
    if (input.tool_name === ASK_USER_TOOL) {
      const decision = askToolDecision(input.tool_input, questionsEnabled);
      return {
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: decision.behavior,
          permissionDecisionReason: decision.message,
        },
      };
    }
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

function securityOptions(rawOptions, policy, questionsEnabled = false) {
  if (!isObject(policy) || policy.version !== POLICY_VERSION) {
    policy = policyForUse(policy);
  }
  const incoming = isObject(rawOptions) ? rawOptions : {};
  const fixedTools = toolsForPolicy(policy, questionsEnabled);
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
        ...(questionsEnabled ? [] : ["AskUserQuestion(*)"]),
        ...(policy.permission === "read-only" ? ["Write(*)", "Edit(*)"] : []),
      ],
    },
  };
  options.tools = [...fixedTools];
  options.allowedTools = [...fixedTools];
  options.disallowedTools = [
    ...FIXED_DISALLOWED_TOOLS.filter((tool) => tool !== ASK_USER_TOOL || !questionsEnabled),
    ...(policy.permission === "read-only" ? [...WRITE_TOOLS] : []),
  ];
  options.mcpServers = {};
  options.additionalDirectories = [];
  options.hooks = {
    PreToolUse: [{ hooks: [createPreToolUseHook(policy, { questionsEnabled })] }],
  };
  return options;
}

export function fixedSessionOptions(rawPolicy, rawOptions = undefined, rawQuestionOptions = undefined) {
  return securityOptions(
    rawOptions,
    policyForUse(rawPolicy),
    rawQuestionOptions?.questionsEnabled === true,
  );
}

function safeMeta(rawMeta, policy, questionsEnabled = false) {
  const meta = isObject(rawMeta) ? { ...rawMeta } : {};
  const claudeCode = isObject(meta.claudeCode) ? { ...meta.claudeCode } : {};
  claudeCode.options = securityOptions(claudeCode.options, policy, questionsEnabled);
  meta.claudeCode = claudeCode;
  delete meta.additionalRoots;
  return meta;
}

export function injectSessionParams(rawParams, rawPolicy, rawQuestionOptions = undefined) {
  if (!isObject(rawParams)) fail("session params must be an object");
  const policy = isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : policyForUse(rawPolicy);
  const questionsEnabled = rawQuestionOptions?.questionsEnabled === true;
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
    _meta: safeMeta(rawParams._meta, policy, questionsEnabled),
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

function wrapSessionMethod(agent, method, policy, questionsEnabled = false) {
  const original = agent[method];
  if (typeof original !== "function") fail(`agent does not expose ${method}`);
  agent[method] = function scopedSessionMethod(params, ...rest) {
    return original.call(agent, injectSessionParams(params, policy, { questionsEnabled }), ...rest);
  };
}

export function installSessionGuards(agent, rawPolicy, rawQuestionOptions = undefined) {
  const policy = isObject(rawPolicy) && rawPolicy.version === POLICY_VERSION
    ? rawPolicy
    : policyForUse(rawPolicy);
  const questionsEnabled = rawQuestionOptions?.questionsEnabled === true;
  for (const method of ["newSession", "loadSession", "resumeSession", "unstable_forkSession"]) {
    wrapSessionMethod(agent, method, policy, questionsEnabled);
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

export function parseMainArgs(args) {
  if (
    !Array.isArray(args) ||
    (args.length !== 4 && args.length !== 5) ||
    args[0] !== "--agent-entry" ||
    args[2] !== "--policy" ||
    (args.length === 5 && args[4] !== "--questions")
  ) {
    fail("usage: --agent-entry /absolute/dist/index.js --policy /absolute/write-policy.json [--questions]");
  }
  return {
    entry: args[1],
    policyPath: args[3],
    questionsEnabled: args.length === 5,
  };
}

async function runMain() {
  const { entry, policyPath, questionsEnabled } = parseMainArgs(process.argv.slice(2));
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
  installSessionGuards(agent, policy, { questionsEnabled });
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
