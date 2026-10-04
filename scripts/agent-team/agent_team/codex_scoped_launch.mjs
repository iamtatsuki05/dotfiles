#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { CODEX_SCOPED_FEATURES } from "./codex_scoped_bridge.mjs";
import { parsePolicy } from "./scoped_policy.mjs";

const MANIFEST_FIELDS = new Set([
  "version", "codex", "codex_sha256", "policy_sha256", "config_sha256",
  "auth_path", "auth_sha256", "auth_expires_at", "model", "effort",
  "instructions", "config_snapshot",
]);
const MAX_PRIVATE_BYTES = 1_048_576;

function fail(message) {
  throw new Error(`codex scoped launch: ${message}`);
}

function text(value, label, multiline = false) {
  if (typeof value !== "string" || !value.length || [...value].some((character) => {
    const code = character.codePointAt(0);
    return code < 32 && (!multiline || ![9, 10, 13].includes(code));
  })) fail(`${label} is invalid`);
  return value;
}

function identity(info) {
  return [info.dev, info.ino, info.mode, info.uid, info.nlink, info.size, info.mtimeNs, info.ctimeNs].join(":");
}

function checkedFile(file, expectedDigest, { privateFile = true, executable = false } = {}) {
  if (typeof file !== "string" || !path.isAbsolute(file) || fs.realpathSync.native(file) !== file) {
    fail("bound artifact path is not canonical");
  }
  if (!/^[0-9a-f]{64}$/u.test(expectedDigest)) fail("artifact digest is invalid");
  const info = fs.lstatSync(file, { bigint: true });
  const owner = BigInt(process.getuid());
  if (!info.isFile() || info.nlink !== 1n ||
      (privateFile ? info.uid !== owner || (info.mode & 0o7777n) !== 0o600n :
        ![0n, owner].includes(info.uid) || (info.mode & 0o022n) !== 0n) ||
      (executable && (info.mode & 0o111n) === 0n) ||
      (privateFile && info.size > BigInt(MAX_PRIVATE_BYTES))) {
    fail("bound artifact ownership, mode, type, or size is unsafe");
  }
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    if (identity(info) !== identity(fs.fstatSync(fd, { bigint: true }))) fail("bound artifact changed while opening");
    const hash = createHash("sha256");
    const chunks = [];
    const buffer = Buffer.alloc(65_536);
    let size = 0;
    for (;;) {
      const count = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (!count) break;
      size += count;
      if (privateFile && size > MAX_PRIVATE_BYTES) fail("private artifact exceeded size limit");
      hash.update(buffer.subarray(0, count));
      if (privateFile) chunks.push(Buffer.from(buffer.subarray(0, count)));
    }
    if (hash.digest("hex") !== expectedDigest ||
        identity(info) !== identity(fs.fstatSync(fd, { bigint: true })) ||
        identity(info) !== identity(fs.lstatSync(file, { bigint: true }))) {
      fail("bound artifact changed since preflight");
    }
    return privateFile ? Buffer.concat(chunks) : undefined;
  } finally {
    fs.closeSync(fd);
  }
}

function checkedDirectory(directory) {
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.uid !== process.getuid() ||
      (info.mode & 0o7777) !== 0o700 || fs.realpathSync.native(directory) !== directory) {
    fail("provider directory must be private, owned, and canonical");
  }
}

function jsonObject(bytes, label) {
  let value;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    fail(`${label} must contain a JSON object`);
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) fail(`${label} must be an object`);
  return value;
}

export function prepareLaunch(manifestPath, manifestDigest, incomingArgs) {
  if (!Array.isArray(incomingArgs) || incomingArgs.length !== 1 || !["app-server", "inspect-config"].includes(incomingArgs[0])) {
    fail("only app-server or inspect-config is allowed");
  }
  const inspection = incomingArgs[0] === "inspect-config";
  const privateRoot = path.dirname(text(manifestPath, "manifest path"));
  if (path.basename(manifestPath) !== "codex-launch.json") fail("manifest filename is invalid");
  checkedDirectory(privateRoot);
  const manifest = jsonObject(checkedFile(manifestPath, manifestDigest), "launch manifest");
  if (Object.keys(manifest).length !== MANIFEST_FIELDS.size ||
      Object.keys(manifest).some((key) => !MANIFEST_FIELDS.has(key)) || manifest.version !== 1) {
    fail("launch manifest fields are invalid");
  }
  const privateHome = path.join(privateRoot, "home");
  const codexHome = path.join(privateRoot, "codex-home");
  const temporary = path.join(privateRoot, "tmp");
  for (const directory of [privateHome, codexHome, temporary]) checkedDirectory(directory);
  const policy = jsonObject(checkedFile(path.join(privateRoot, "write-policy.json"), manifest.policy_sha256), "write policy");
  const normalized = parsePolicy(policy);
  if (!normalized.protected_paths.includes(privateRoot) ||
      !normalized.protected_paths.includes(path.dirname(fileURLToPath(import.meta.url)))) {
    fail("policy must protect provider state and scoped runtime");
  }
  const authPath = text(manifest.auth_path, "authentication path");
  if (!normalized.protected_paths.some((root) => {
    const relative = path.relative(root, authPath);
    return relative === "" || !path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`);
  })) {
    fail("policy must protect the authentication source");
  }
  checkedFile(path.join(codexHome, "config.toml"), manifest.config_sha256);
  checkedFile(manifest.codex, manifest.codex_sha256, { privateFile: false, executable: true });
  checkedFile(manifest.auth_path, manifest.auth_sha256);
  const authLink = path.join(codexHome, "auth.json");
  if (!fs.lstatSync(authLink).isSymbolicLink() || fs.readlinkSync(authLink) !== manifest.auth_path) {
    fail("private Codex authentication link does not match preflight");
  }
  if (!Number.isSafeInteger(manifest.auth_expires_at) || manifest.auth_expires_at <= Date.now() / 1_000 + 300) {
    fail("authentication startup binding has expired");
  }
  const model = text(manifest.model, "model");
  const effort = text(manifest.effort, "effort");
  const instructions = text(manifest.instructions, "instructions", true);
  if (inspection ? manifest.config_snapshot !== null : !/^[0-9a-f]{64}$/u.test(manifest.config_snapshot)) {
    fail("config snapshot does not match the launch phase");
  }
  const bridgeOptions = { policy, model, effort, instructions, configSnapshot: manifest.config_snapshot };
  const overrides = {
    model,
    model_provider: "openai",
    model_reasoning_effort: effort,
    cli_auth_credentials_store: "file",
    approval_policy: "never",
    approvals_reviewer: "user",
    web_search: "disabled",
    "history.persistence": "none",
    project_doc_max_bytes: 0,
    notify: [],
    check_for_update_on_startup: false,
    sqlite_home: codexHome,
    log_dir: path.join(codexHome, "log"),
    "skills.include_instructions": false,
    "skills.bundled.enabled": false,
    "analytics.enabled": false,
    "otel.exporter": "none",
    "otel.trace_exporter": "none",
    "otel.metrics_exporter": "none",
    project_root_markers: [".git"],
    ...Object.fromEntries(Object.entries(CODEX_SCOPED_FEATURES).map(([key, value]) => [`features.${key}`, value])),
  };
  return {
    inspection,
    command: manifest.codex,
    argv: [...Object.entries(overrides).flatMap(([key, value]) => ["--config", `${key}=${JSON.stringify(value)}`]), "app-server"],
    cwd: normalized.workspace,
    env: {
      HOME: privateHome,
      CODEX_HOME: codexHome,
      TMPDIR: temporary,
      XDG_CONFIG_HOME: path.join(privateHome, ".config"),
      XDG_DATA_HOME: path.join(privateHome, ".local", "share"),
      XDG_CACHE_HOME: path.join(privateHome, ".cache"),
      PATH: "/usr/bin:/bin",
      SHELL: "/bin/sh",
      LANG: "C.UTF-8",
    },
    bridgeOptions,
  };
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 5 || args[0] !== "--manifest" || args[2] !== "--sha256") {
    fail("expected --manifest PATH --sha256 DIGEST app-server|inspect-config");
  }
  const plan = prepareLaunch(args[1], args[3], args.slice(4));
  if (plan.inspection) {
    const { inspectConfig } = await import("./codex_scoped_inspect.mjs");
    const child = spawn(plan.command, plan.argv, { cwd: plan.cwd, env: plan.env, stdio: ["pipe", "pipe", "pipe"] });
    const result = await inspectConfig({ child, workspace: plan.cwd, model: plan.bridgeOptions.model, effort: plan.bridgeOptions.effort });
    await new Promise((resolve, reject) => {
      process.stdout.write(`${JSON.stringify(result)}\n`, (error) => error ? reject(error) : resolve());
    });
  } else {
    const { runTransport } = await import("./codex_scoped_transport.mjs");
    const child = spawn(plan.command, plan.argv, { cwd: plan.cwd, env: plan.env, stdio: ["pipe", "pipe", "pipe"] });
    const result = await runTransport({ child, input: process.stdin, output: process.stdout, bridgeOptions: plan.bridgeOptions });
    process.exitCode = result.exitCode;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => {
    process.stderr.write("codex scoped launch failed; bound configuration or transport could not be verified\n");
    process.exitCode = 1;
  });
}
