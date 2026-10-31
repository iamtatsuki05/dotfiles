#!/usr/bin/env node

import { createHash } from "node:crypto";

import { DYNAMIC_FILE_TOOLS, executeFileTool } from "./scoped_file_tools.mjs";
import { parsePolicy } from "./scoped_policy.mjs";

const CLIENT_METHODS = new Set([
  "initialize",
  "account/read",
  "config/read",
  "skills/list",
  "model/list",
  "thread/start",
  "turn/start",
  "turn/interrupt",
  "thread/unsubscribe",
]);

const FIXED_CLIENT_INFO = Object.freeze({
  name: "@agentclientprotocol/codex-acp",
  title: "Codex ACP",
  version: "1.10.0",
});

export const CODEX_SCOPED_FEATURES = Object.freeze({
  shell_tool: false,
  view_image: false,
  unified_exec: false,
  deferred_executor: false,
  request_permissions_tool: false,
  hooks: false,
  code_mode: false,
  code_mode_host: false,
  code_mode_only: false,
  code_mode_prewarm: false,
  multi_agent: false,
  multi_agent_v2: false,
  apps: false,
  enable_mcp_apps: false,
  tool_suggest: false,
  plugins: false,
  recommended_plugins: false,
  plugin_sharing: false,
  image_generation: false,
  standalone_web_search: false,
  remote_plugin: false,
  skill_mcp_dependency_install: false,
  goals: false,
  skip_host_skill_discovery: true,
});

const READ_ONLY_TOOL_NAMES = new Set(["read_text", "list_files"]);
const ALL_TOOL_NAMES = new Set(DYNAMIC_FILE_TOOLS.map((tool) => tool.name));

function fail(message) {
  throw new Error(`codex scoped bridge: ${message}`);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function assertObject(value, label) {
  if (!isObject(value)) fail(`${label} must be an object`);
  return value;
}

function assertString(value, label, { allowEmpty = false } = {}) {
  if (typeof value !== "string" || (!allowEmpty && value.length === 0) || value.includes("\0")) {
    fail(`${label} must be a ${allowEmpty ? "string" : "non-empty string"} without NUL`);
  }
  return value;
}

function assertText(value, label, { allowEmpty = false } = {}) {
  const result = assertString(value, label, { allowEmpty });
  if ([...result].some((character) => {
    const code = character.codePointAt(0);
    return code !== undefined && code < 0x20 && code !== 0x09 && code !== 0x0a && code !== 0x0d;
  })) {
    fail(`${label} must not contain control characters`);
  }
  return result;
}

function assertExactKeys(value, allowed, required, label) {
  assertObject(value, label);
  const allowedSet = new Set(allowed);
  const requiredSet = new Set(required);
  for (const key of Object.keys(value)) {
    if (!allowedSet.has(key)) fail(`${label} contains unknown field ${key}`);
  }
  for (const key of requiredSet) {
    if (!hasOwn(value, key)) fail(`${label} is missing ${key}`);
  }
  return value;
}

function assertBoolean(value, label) {
  if (typeof value !== "boolean") fail(`${label} must be a boolean`);
  return value;
}

function assertNullableString(value, label) {
  if (value !== null) return assertString(value, label);
  return value;
}

function cloneJson(value, label) {
  let encoded;
  try {
    encoded = JSON.stringify(value);
  } catch (error) {
    fail(`${label} is not JSON serializable: ${error?.message ?? error}`);
  }
  if (encoded === undefined) fail(`${label} is not JSON serializable`);
  return JSON.parse(encoded);
}

function validateJsonValue(value, label, ancestors = new Set()) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail(`${label} contains a non-finite number`);
    return;
  }
  if (typeof value !== "object") fail(`${label} contains a non-JSON value`);
  if (ancestors.has(value)) fail(`${label} contains a cycle`);
  const nextAncestors = new Set(ancestors);
  nextAncestors.add(value);
  if (Array.isArray(value)) {
    value.forEach((child, index) => validateJsonValue(child, `${label}[${index}]`, nextAncestors));
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    validateJsonValue(child, `${label}.${key}`, nextAncestors);
  }
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map((child) => canonicalize(child));
  if (isObject(value)) {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
}

export function configSnapshot(response) {
  const value = assertObject(response, "config/read response");
  if (!hasOwn(value, "config") || !isObject(value.config)) {
    fail("config/read response must contain an object config");
  }
  if (!hasOwn(value, "origins") || !isObject(value.origins)) {
    fail("config/read response must contain an object origins");
  }
  if (hasOwn(value, "layers") && value.layers !== null && !Array.isArray(value.layers)) {
    fail("config/read response layers must be an array or null");
  }
  validateJsonValue(value, "config/read response");
  const canonical = JSON.stringify(canonicalize(value));
  return createHash("sha256").update(canonical, "utf8").digest("hex");
}

function validateConfigMcpServers(value, label) {
  if (value === undefined || value === null) return [];
  if (!isObject(value)) fail(`${label} must be an object or null`);
  return Object.keys(value);
}

function mcpNamesFromConfig(response) {
  const names = new Set();
  const addConfig = (value, label) => {
    if (!isObject(value)) return;
    for (const name of validateConfigMcpServers(value.mcp_servers, `${label}.mcp_servers`)) {
      names.add(name);
    }
  };
  addConfig(response.config, "config");
  if (Array.isArray(response.layers)) {
    response.layers.forEach((layer, index) => {
      if (layer === null || layer === undefined) return;
      if (!isObject(layer)) fail(`config/read response layers[${index}] must be an object`);
      addConfig(layer.config, `layers[${index}].config`);
    });
  }
  return [...names].sort();
}

function fixedDynamicTools(permission) {
  const names = permission === "read-only" ? READ_ONLY_TOOL_NAMES : ALL_TOOL_NAMES;
  return DYNAMIC_FILE_TOOLS
    .filter((tool) => names.has(tool.name))
    .map((tool) => cloneJson(tool, "dynamic tool definition"));
}

function validateModelListResponse(response, label) {
  const value = assertObject(response, label);
  if (!Array.isArray(value.data)) fail(`${label} must contain data[]`);
  if (hasOwn(value, "nextCursor") && value.nextCursor !== null && typeof value.nextCursor !== "string") {
    fail(`${label}.nextCursor must be a string or null`);
  }
  return value;
}

function modelEntryMatches(entry, model, effort) {
  if (!isObject(entry)) return false;
  const modelMatch = entry.id === model || entry.model === model;
  if (!modelMatch) return false;
  if (!Array.isArray(entry.supportedReasoningEfforts)) return false;
  return entry.supportedReasoningEfforts.some(
    (item) => isObject(item) && item.reasoningEffort === effort,
  );
}

function validateInputBlocks(input) {
  if (!Array.isArray(input) || input.length === 0) fail("turn input must contain text blocks");
  return input.map((block, index) => {
    assertExactKeys(block, ["type", "text", "text_elements"], ["type", "text", "text_elements"], `turn input[${index}]`);
    if (block.type !== "text") fail("turn input may contain only text blocks");
    const text = assertText(block.text, `turn input[${index}].text`);
    if (!Array.isArray(block.text_elements) || block.text_elements.length !== 0) {
      fail("turn text_elements must be empty arrays");
    }
    return { type: "text", text, text_elements: [] };
  });
}

function validateApprovalPolicy(value, label) {
  if (value === undefined || value === null) return;
  if (typeof value === "string") {
    if (!["untrusted", "on-request", "never"].includes(value)) fail(`${label} is invalid`);
    return;
  }
  const object = assertObject(value, label);
  assertExactKeys(object, ["granular"], ["granular"], label);
  const granular = assertObject(object.granular, `${label}.granular`);
  assertExactKeys(
    granular,
    ["mcp_elicitations", "request_permissions", "rules", "sandbox_approval", "skill_approval"],
    ["mcp_elicitations", "rules", "sandbox_approval"],
    `${label}.granular`,
  );
  for (const key of Object.keys(granular)) assertBoolean(granular[key], `${label}.granular.${key}`);
}

function validateSandboxPolicy(value) {
  if (value === undefined || value === null) return;
  const policy = assertObject(value, "turn sandboxPolicy");
  assertExactKeys(
    policy,
    ["type", "writableRoots", "networkAccess", "excludeTmpdirEnvVar", "excludeSlashTmp"],
    ["type", "writableRoots", "networkAccess", "excludeTmpdirEnvVar", "excludeSlashTmp"],
    "turn sandboxPolicy",
  );
  if (!["readOnly", "workspaceWrite"].includes(policy.type)) fail("turn sandboxPolicy type is not allowed");
  if (!Array.isArray(policy.writableRoots) || policy.writableRoots.length !== 0) {
    fail("turn sandboxPolicy writableRoots must be empty");
  }
  policy.writableRoots.forEach((root, index) => assertString(root, `turn sandboxPolicy writableRoots[${index}]`));
  assertBoolean(policy.networkAccess, "turn sandboxPolicy networkAccess");
  assertBoolean(policy.excludeTmpdirEnvVar, "turn sandboxPolicy excludeTmpdirEnvVar");
  assertBoolean(policy.excludeSlashTmp, "turn sandboxPolicy excludeSlashTmp");
  if (policy.networkAccess) fail("turn sandboxPolicy network access is not allowed");
}

function validateTurnParams(params, threadId, model, effort) {
  const value = assertExactKeys(
    params,
    ["threadId", "input", "model", "effort", "approvalPolicy", "approvalsReviewer", "sandboxPolicy", "summary", "serviceTier"],
    ["threadId", "input"],
    "turn/start params",
  );
  if (value.threadId !== threadId) fail("turn/start threadId does not match the current thread");
  const input = validateInputBlocks(value.input);
  if (value.model !== undefined && value.model !== null && value.model !== model) {
    fail("turn/start model does not match the selected model");
  }
  if (value.effort !== undefined && value.effort !== null && value.effort !== effort) {
    fail("turn/start effort does not match the selected effort");
  }
  validateApprovalPolicy(value.approvalPolicy, "turn approvalPolicy");
  if (value.approvalsReviewer !== undefined && value.approvalsReviewer !== null &&
      !["user", "auto_review", "guardian_subagent"].includes(value.approvalsReviewer)) {
    fail("turn approvalsReviewer is invalid");
  }
  validateSandboxPolicy(value.sandboxPolicy);
  if (value.summary !== undefined && value.summary !== null && !["auto", "none"].includes(value.summary)) {
    fail("turn summary is invalid");
  }
  if (value.serviceTier !== undefined && value.serviceTier !== null) {
    fail("turn serviceTier override is not allowed");
  }
  return input;
}

function toolFailure(message) {
  return {
    contentItems: [{ type: "inputText", text: JSON.stringify({ error: message }) }],
    success: false,
  };
}

function toolSuccess(value) {
  return {
    contentItems: [{ type: "inputText", text: JSON.stringify(value) }],
    success: true,
  };
}

export class CodexScopeBridge {
  constructor({ policy, model, effort, instructions, configSnapshot: expectedSnapshot }, requestChild) {
    if (typeof requestChild !== "function") fail("requestChild must be a function");
    this.rawPolicy = cloneJson(policy, "policy");
    this.policy = parsePolicy(this.rawPolicy);
    this.model = assertString(model, "model");
    this.effort = assertString(effort, "effort");
    this.instructions = assertText(instructions, "instructions");
    this.expectedSnapshot = assertString(expectedSnapshot, "configSnapshot");
    if (!/^[0-9a-f]{64}$/u.test(this.expectedSnapshot)) fail("configSnapshot must be a SHA-256 hex digest");
    this.requestChild = requestChild;
    this.initialized = false;
    this.threadStartAttempted = false;
    this.threadStarted = false;
    this.threadId = null;
    this.turnStartAttempted = false;
    this.turnStarted = false;
    this.turnId = null;
    this.turnStartPending = false;
    this.pendingTurnId = null;
    this.pendingTurnCompleted = false;
    this.turnCompleted = false;
    this.interruptRequested = false;
    this.closed = false;
    this.callIds = new Set();
    this.mcpServerNames = [];
    this.modelEntries = [];
    this.modelCatalogComplete = false;
  }

  async request(method, params) {
    return await this.requestChild(method, params);
  }

  requireInitialized() {
    if (!this.initialized) fail("initialize is required before this request");
    if (this.closed) fail("bridge is closed");
  }

  async handleClientRequest(method, params) {
    if (typeof method !== "string" || !CLIENT_METHODS.has(method)) {
      fail(`unknown client method: ${String(method)}`);
    }
    if (method !== "initialize") this.requireInitialized();
    switch (method) {
      case "initialize":
        return await this.handleInitialize(params);
      case "account/read":
        return await this.handleAccountRead(params);
      case "config/read":
        return await this.handleConfigRead(params);
      case "skills/list":
        return this.handleSkillsList(params);
      case "model/list":
        return await this.handleModelList(params);
      case "thread/start":
        return await this.handleThreadStart(params);
      case "turn/start":
        return await this.handleTurnStart(params);
      case "turn/interrupt":
        return await this.handleTurnInterrupt(params);
      case "thread/unsubscribe":
        return await this.handleThreadUnsubscribe(params);
      default:
        fail(`unknown client method: ${method}`);
    }
  }

  async handleInitialize(params) {
    if (this.initialized) fail("initialize may only be requested once");
    const value = assertExactKeys(params, ["clientInfo", "capabilities"], ["clientInfo"], "initialize params");
    assertObject(value.clientInfo, "initialize clientInfo");
    if (hasOwn(value, "capabilities") && value.capabilities !== null) assertObject(value.capabilities, "initialize capabilities");
    const response = await this.request("initialize", {
      capabilities: { experimentalApi: true, requestAttestation: false },
      clientInfo: { ...FIXED_CLIENT_INFO },
    });
    this.initialized = true;
    return response;
  }

  async handleAccountRead(params) {
    const value = assertExactKeys(params, ["refreshToken"], ["refreshToken"], "account/read params");
    if (value.refreshToken !== false) fail("account/read refreshToken must be false");
    const response = await this.request("account/read", { refreshToken: false });
    if (!isObject(response) || !isObject(response.account)) fail("auth_required: ChatGPT account is required");
    if (response.account.type !== "chatgpt") fail("unsupported account type");
    return response;
  }

  validateConfigReadParams(params) {
    const value = assertExactKeys(params, ["cwd", "includeLayers"], [], "config/read params");
    if (value.cwd !== undefined && value.cwd !== null && value.cwd !== this.policy.workspace) {
      fail("config/read cwd does not match the policy workspace");
    }
    if (value.includeLayers !== undefined) assertBoolean(value.includeLayers, "config/read includeLayers");
  }

  async readAndCheckConfig() {
    const response = await this.request("config/read", { cwd: this.policy.workspace, includeLayers: true });
    const snapshot = configSnapshot(response);
    if (snapshot !== this.expectedSnapshot) fail("config snapshot drift detected");
    const config = response.config;
    if (config.model_provider !== undefined && config.model_provider !== null && config.model_provider !== "openai") {
      fail("custom model provider is not supported");
    }
    if (config.model !== undefined && config.model !== null && config.model !== this.model) {
      fail("config model does not match the selected model");
    }
    if (config.model_reasoning_effort !== undefined && config.model_reasoning_effort !== null && config.model_reasoning_effort !== this.effort) {
      fail("config reasoning effort does not match the selected effort");
    }
    this.mcpServerNames = mcpNamesFromConfig(response);
    return response;
  }

  async handleConfigRead(params) {
    this.validateConfigReadParams(params);
    await this.readAndCheckConfig();
    return { config: { model_provider: "openai" }, origins: {}, layers: [] };
  }

  handleSkillsList(params) {
    const value = assertExactKeys(params, ["cwds", "forceReload"], ["cwds"], "skills/list params");
    if (!Array.isArray(value.cwds) || value.cwds.length !== 1 || value.cwds[0] !== this.policy.workspace) {
      fail("skills/list may use only the policy workspace cwd");
    }
    if (value.forceReload !== undefined) assertBoolean(value.forceReload, "skills/list forceReload");
    return { data: [{ cwd: this.policy.workspace, skills: [], errors: [] }] };
  }

  async handleModelList(params) {
    const value = assertExactKeys(params, ["cursor", "limit"], [], "model/list params");
    const cursor = value.cursor === undefined ? null : value.cursor;
    const limit = value.limit === undefined ? null : value.limit;
    assertNullableString(cursor, "model/list cursor");
    if (limit !== null && (!Number.isSafeInteger(limit) || limit < 0)) fail("model/list limit must be a non-negative integer or null");
    const response = validateModelListResponse(await this.request("model/list", { cursor, limit }), "model/list response");
    this.modelEntries.push(...response.data);
    const nextCursor = response.nextCursor === undefined ? null : response.nextCursor;
    if (nextCursor === null) {
      this.modelCatalogComplete = true;
      if (!this.modelEntries.some((entry) => modelEntryMatches(entry, this.model, this.effort))) {
        fail("selected model and effort are absent from model/list");
      }
    }
    return response;
  }

  validateThreadStartParams(params) {
    const value = assertExactKeys(params, ["cwd", "modelProvider", "config"], ["cwd", "modelProvider", "config"], "thread/start params");
    if (value.cwd !== this.policy.workspace) fail("thread/start cwd does not match the policy workspace");
    if (value.modelProvider !== null && value.modelProvider !== "openai") fail("thread/start modelProvider is not allowed");
    const config = assertExactKeys(value.config, ["projects"], ["projects"], "thread/start config");
    const projects = assertExactKeys(config.projects, [this.policy.workspace], [this.policy.workspace], "thread/start config.projects");
    const project = assertExactKeys(projects[this.policy.workspace], ["trust_level"], ["trust_level"], "thread/start project");
    if (project.trust_level !== "trusted") fail("thread/start project trust_level must be trusted");
  }

  async handleThreadStart(params) {
    if (this.threadStartAttempted) fail("only one thread/start attempt is allowed");
    this.validateThreadStartParams(params);
    this.threadStartAttempted = true;
    const requirements = await this.request("configRequirements/read", {});
    if (!isObject(requirements) || requirements.requirements !== null) fail("managed config requirements are not supported");
    await this.readAndCheckConfig();
    const disabledMcp = Object.fromEntries(this.mcpServerNames.map((name) => [name, { enabled: false }]));
    const childParams = {
      cwd: this.policy.workspace,
      model: this.model,
      modelProvider: "openai",
      permissions: ":read-only",
      approvalPolicy: "never",
      approvalsReviewer: "user",
      environments: [],
      ephemeral: true,
      allowProviderModelFallback: false,
      dynamicTools: fixedDynamicTools(this.policy.permission),
      developerInstructions: `${this.instructions}\n提供されたファイル操作ツールで操作してください。`,
      config: {
        projects: { [this.policy.workspace]: { trust_level: "trusted" } },
        model: this.model,
        model_reasoning_effort: this.effort,
        mcp_servers: disabledMcp,
        features: { ...CODEX_SCOPED_FEATURES },
        web_search: "disabled",
        project_doc_max_bytes: 0,
        notify: [],
        skills: { include_instructions: false, bundled: { enabled: false } },
      },
    };
    const response = await this.request("thread/start", childParams);
    this.validateThreadStartResponse(response);
    this.threadId = response.thread.id;
    this.threadStarted = true;
    return response;
  }

  validateThreadStartResponse(response) {
    const value = assertObject(response, "thread/start response");
    if (!isObject(value.thread) || typeof value.thread.id !== "string" || value.thread.id.length === 0) fail("thread/start response omitted thread id");
    if (value.cwd !== this.policy.workspace || value.thread.cwd !== this.policy.workspace) fail("thread/start response cwd does not match the workspace");
    if (value.model !== this.model) fail("thread/start response model does not match the selected model");
    if (value.thread.model != null && value.thread.model !== this.model) fail("thread/start thread model conflicts with the selected model");
    if (value.modelProvider !== "openai") fail("thread/start response modelProvider is not openai");
    if (value.reasoningEffort !== this.effort) fail("thread/start response effort does not match the selected effort");
    if (value.thread.reasoningEffort != null && value.thread.reasoningEffort !== this.effort) fail("thread/start thread effort conflicts with the selected effort");
    if (value.approvalPolicy !== "never") fail("thread/start response approvalPolicy is not never");
    if (value.approvalsReviewer !== "user") fail("thread/start response approvalsReviewer is not user");
    if (value.thread.ephemeral !== true) fail("thread/start response thread is not ephemeral");
    if (!isObject(value.activePermissionProfile) || value.activePermissionProfile.id !== ":read-only" || value.activePermissionProfile.extends !== null) {
      fail("thread/start response permission profile is not :read-only");
    }
  }

  async handleTurnStart(params) {
    if (!this.threadStarted) fail("thread/start must succeed before turn/start");
    if (this.turnStartAttempted) fail("only one turn/start attempt is allowed");
    const input = validateTurnParams(params, this.threadId, this.model, this.effort);
    this.turnStartAttempted = true;
    this.turnStartPending = true;
    this.pendingTurnId = null;
    this.pendingTurnCompleted = false;
    const response = await this.request("turn/start", {
      threadId: this.threadId,
      input,
      model: this.model,
      effort: this.effort,
      permissions: ":read-only",
      approvalPolicy: "never",
      environments: [],
    });
    const value = assertObject(response, "turn/start response");
    if (!isObject(value.turn) || typeof value.turn.id !== "string" || value.turn.id.length === 0) fail("turn/start response omitted turn id");
    if (this.pendingTurnId !== null && this.pendingTurnId !== value.turn.id) fail("turn/started notification id does not match turn/start result");
    this.turnId = value.turn.id;
    this.turnStarted = true;
    this.turnStartPending = false;
    this.turnCompleted = this.pendingTurnCompleted;
    this.pendingTurnId = null;
    this.pendingTurnCompleted = false;
    return response;
  }

  async handleTurnInterrupt(params) {
    const currentTurnId = this.turnId ?? this.pendingTurnId;
    if (!this.threadStarted || (!this.turnStarted && !this.turnStartPending) || this.closed || currentTurnId === null) {
      fail("turn/interrupt requires the current active thread and known turn id");
    }
    const value = assertExactKeys(params, ["threadId", "turnId"], ["threadId", "turnId"], "turn/interrupt params");
    if (value.threadId !== this.threadId || value.turnId !== currentTurnId) fail("turn/interrupt ids do not match the current turn");
    if (this.turnCompleted || this.pendingTurnCompleted || this.interruptRequested) return {};
    this.interruptRequested = true;
    return await this.request("turn/interrupt", { threadId: this.threadId, turnId: currentTurnId });
  }

  async handleThreadUnsubscribe(params) {
    if (!this.threadStarted) fail("thread/unsubscribe requires a current thread");
    const value = assertExactKeys(params, ["threadId"], ["threadId"], "thread/unsubscribe params");
    if (value.threadId !== this.threadId) fail("thread/unsubscribe id does not match the current thread");
    if (this.closed) return {};
    this.closed = true;
    return await this.request("thread/unsubscribe", { threadId: this.threadId });
  }

  handleNotification(method, params) {
    if (method === "turn/started") {
      if (!isObject(params) || params.threadId !== this.threadId || !isObject(params.turn) || typeof params.turn.id !== "string") return undefined;
      if (this.closed || this.turnCompleted || this.interruptRequested) return undefined;
      if (!this.turnStartPending && !this.turnStarted) return undefined;
      if (this.pendingTurnId !== null && this.pendingTurnId !== params.turn.id) return undefined;
      if (this.turnId !== null && this.turnId !== params.turn.id) return undefined;
      if (this.turnStartPending) this.pendingTurnId = params.turn.id;
      else this.turnId = params.turn.id;
      return params;
    }
    if (method === "turn/completed") {
      if (!isObject(params) || params.threadId !== this.threadId || !isObject(params.turn) || typeof params.turn.id !== "string") return undefined;
      const currentId = this.pendingTurnId ?? this.turnId;
      if (currentId === null || currentId !== params.turn.id || this.closed) return undefined;
      if (this.turnStartPending) this.pendingTurnCompleted = true;
      else this.turnCompleted = true;
      return params;
    }
    if (method === "error") {
      if (!isObject(params) || params.threadId !== this.threadId || typeof params.turnId !== "string") return undefined;
      const currentId = this.pendingTurnId ?? this.turnId;
      if (currentId === null || currentId !== params.turnId || this.closed) return undefined;
      if (this.turnStartPending) this.pendingTurnCompleted = true;
      else this.turnCompleted = true;
      return params;
    }
    return params;
  }

  requireActiveTurn() {
    if (!this.threadStarted || !this.turnStarted || this.closed || this.turnCompleted || this.interruptRequested) {
      fail("dynamic tool call is not allowed after the turn ended");
    }
  }

  async handleServerRequest(method, params) {
    if (method === "item/tool/call") return await this.handleDynamicToolCall(params);
    if (method === "item/commandExecution/requestApproval" || method === "item/fileChange/requestApproval") return { decision: "cancel" };
    if (method === "item/permissions/requestApproval") return { permissions: {}, scope: "turn", strictAutoReview: false };
    if (method === "mcpServer/elicitation/request") return { action: "cancel", content: null, _meta: null };
    if (method === "item/tool/requestUserInput") return { answers: {} };
    fail(`unsupported server request: ${String(method)}`);
  }

  async handleDynamicToolCall(params) {
    this.requireActiveTurn();
    const value = assertExactKeys(params, ["arguments", "callId", "namespace", "threadId", "tool", "turnId"], ["arguments", "callId", "threadId", "tool", "turnId"], "item/tool/call params");
    if (value.namespace !== undefined && value.namespace !== null) fail("dynamic tool namespace is not allowed");
    if (value.threadId !== this.threadId || value.turnId !== this.turnId) fail("dynamic tool ids do not match the current turn");
    const callId = assertString(value.callId, "dynamic tool callId");
    if (this.callIds.has(callId)) fail("dynamic tool callId was already used");
    this.callIds.add(callId);
    const tool = assertString(value.tool, "dynamic tool name");
    if (!ALL_TOOL_NAMES.has(tool) || (this.policy.permission === "read-only" && !READ_ONLY_TOOL_NAMES.has(tool))) {
      return toolFailure("dynamic tool is not allowed");
    }
    try {
      return toolSuccess(await executeFileTool(this.rawPolicy, tool, value.arguments));
    } catch (error) {
      return toolFailure(`dynamic tool execution failed: ${error?.message ?? error}`);
    }
  }
}
