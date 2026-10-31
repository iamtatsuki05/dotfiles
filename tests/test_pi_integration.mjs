import assert from "node:assert/strict";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const piAppDir = join(repoRoot, "dotfiles/.agent/apps/pi");
const settingsPath = join(piAppDir, "settings.json");
const mcpPath = join(piAppDir, "mcp.json");
const hooksDir = join(repoRoot, "dotfiles/.agent/hooks");
const dotfilesExtensionPath = join(piAppDir, "extensions/dotfiles.ts");
const rtkExtensionPath = join(piAppDir, "extensions/rtk.ts");

function readJson(path, label) {
  assert.ok(existsSync(path), `${label} is missing`);
  return JSON.parse(readFileSync(path, "utf8"));
}

function makeFixture(t) {
  const root = mkdtempSync(join(process.env.TMPDIR ?? "/tmp", "pi integration fixture "));
  const agentDir = join(root, "agent dir");
  const hooks = join(agentDir, "hooks");
  const workspace = join(root, "workspace with spaces");
  const bin = join(root, "bin");
  mkdirSync(hooks, { recursive: true });
  mkdirSync(workspace, { recursive: true });
  mkdirSync(bin, { recursive: true });

  const saved = new Map();
  const setEnv = (key, value) => {
    if (!saved.has(key)) saved.set(key, process.env[key]);
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  };
  setEnv("PI_CODING_AGENT_DIR", agentDir);
  setEnv("PATH", `${bin}${delimiter}${process.env.PATH ?? ""}`);

  const environmentKeys = ["PI_CODING_AGENT_DIR", "PATH", "PI_CONTEXT_INPUT", "PI_NOTIFY_LOG", "JUPYTEXT_LOG"];
  for (const key of environmentKeys) if (!saved.has(key)) saved.set(key, process.env[key]);
  t.after(() => {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(root, { recursive: true, force: true });
  });

  return { root, agentDir, hooks, workspace, bin, setEnv };
}

function writeExecutable(path, content) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, content, { mode: 0o755 });
  chmodSync(path, 0o755);
}

function linkHook(fixture, hookName) {
  symlinkSync(join(hooksDir, hookName), join(fixture.hooks, hookName));
}

async function loadExtension(path) {
  assert.ok(existsSync(path), `extension is missing: ${path}`);
  const extension = await import(pathToFileURL(path).href);
  assert.equal(typeof extension.default, "function", `extension has no default factory: ${path}`);
  return extension.default;
}

function createExtensionHost(overrides = {}) {
  const handlers = new Map();
  const pi = {
    on(event, handler) {
      assert.ok(!handlers.has(event), `duplicate handler for ${event}`);
      handlers.set(event, handler);
      return () => handlers.delete(event);
    },
    ...overrides,
  };
  return { pi, handlers };
}

function toolResult(toolName, input, options = {}) {
  return {
    type: "tool_result",
    toolCallId: "call-1",
    toolName,
    input,
    content: [{ type: "text", text: "tool output" }],
    isError: false,
    ...options,
  };
}

test("Pi settings select the requested OpenAI model and thinking level", () => {
  const settings = readJson(settingsPath, "Pi settings.json");
  assert.equal(settings.defaultProvider, "openai");
  assert.equal(settings.defaultModel, "gpt-6-luna");
  assert.equal(settings.defaultThinkingLevel, "high");
  assert.equal("auth" in settings, false);
  assert.equal("apiKey" in settings, false);
});

test("Pi MCP config mirrors the supported Codex servers without approval-only fields", () => {
  const config = readJson(mcpPath, "Pi mcp.json");
  const servers = config.mcpServers;
  assert.deepEqual(Object.keys(servers).sort(), ["deepwiki", "devin", "figma", "notion", "playwright"]);
  assert.deepEqual(servers.deepwiki, {
    type: "streamable-http",
    url: "https://mcp.deepwiki.com/mcp",
  });
  assert.deepEqual(servers.devin, {
    type: "streamable-http",
    url: "https://mcp.devin.ai/mcp",
    headers: { Authorization: "Bearer ${DEVIN_API_KEY}" },
  });
  assert.deepEqual(servers.figma, {
    type: "streamable-http",
    url: "https://mcp.figma.com/mcp",
  });
  assert.deepEqual(servers.notion, {
    type: "streamable-http",
    url: "https://mcp.notion.com/mcp",
  });
  assert.deepEqual(servers.playwright, {
    type: "stdio",
    command: "bunx",
    args: ["@playwright/mcp@latest"],
    enabled: false,
  });
  assert.equal(JSON.stringify(config).includes("approval_mode"), false);
});

test("RTK Pi extension is the pinned upstream v0.51.0 source", async () => {
  assert.ok(existsSync(rtkExtensionPath), "Pi RTK extension is missing");
  const digest = createHash("sha256").update(readFileSync(rtkExtensionPath)).digest("hex");
  assert.equal(digest, "d1555e0af5872a30ed04059423350bdb38302f200a9642c5a9bcc519c95a2257");
});

test("RTK mutates only the bash command and does not return a deny decision", async () => {
  const calls = [];
  const { pi, handlers } = createExtensionHost({
    async exec(command, args, options) {
      calls.push({ command, args, options });
      if (args[0] === "--version") return { stdout: "rtk 0.51.0", stderr: "", code: 0, killed: false };
      if (args[0] === "rewrite") return { stdout: "rtk git status\n", stderr: "", code: 0, killed: false };
      throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
    },
  });
  const extension = await loadExtension(rtkExtensionPath);
  await extension(pi);

  const event = {
    type: "tool_call",
    toolCallId: "call-1",
    toolName: "bash",
    input: { command: "git status" },
  };
  const result = await handlers.get("tool_call")(event, { signal: undefined });
  assert.equal(event.input.command, "rtk git status");
  assert.equal(result, undefined);
  assert.equal(calls.filter((call) => call.args[0] === "rewrite").length, 1);
});

test("RTK passes through non-bash calls, existing rtk commands, and rewrite misses", async (t) => {
  const original = process.env.RTK_DISABLED;
  delete process.env.RTK_DISABLED;
  t.after(() => {
    if (original === undefined) delete process.env.RTK_DISABLED;
    else process.env.RTK_DISABLED = original;
  });
  const calls = [];
  const { pi, handlers } = createExtensionHost({
    async exec(command, args) {
      calls.push(args);
      if (args[0] === "--version") return { stdout: "rtk 0.51.0", stderr: "", code: 0, killed: false };
      return { stdout: "", stderr: "", code: 1, killed: false };
    },
  });
  await (await loadExtension(rtkExtensionPath))(pi);
  const handle = handlers.get("tool_call");

  const read = { type: "tool_call", toolCallId: "1", toolName: "read", input: { path: "README.md" } };
  assert.equal(await handle(read, { signal: undefined }), undefined);
  const alreadyRewritten = { type: "tool_call", toolCallId: "2", toolName: "bash", input: { command: "rtk git status" } };
  assert.equal(await handle(alreadyRewritten, { signal: undefined }), undefined);
  const miss = { type: "tool_call", toolCallId: "3", toolName: "bash", input: { command: "some command" } };
  assert.equal(await handle(miss, { signal: undefined }), undefined);
  assert.equal(calls.filter((args) => args[0] === "rewrite").length, 1);
  assert.equal(miss.input.command, "some command");
});

test("context reminder uses Pi before_agent_start and the configured agent directory", async (t) => {
  const fixture = makeFixture(t);
  const inputLog = join(fixture.root, "context-input.json");
  fixture.setEnv("PI_CONTEXT_INPUT", inputLog);
  writeExecutable(join(fixture.hooks, "agent_context_reminder.sh"), `#!/bin/sh
cat > "$PI_CONTEXT_INPUT"
printf '%s\\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"repo reminder"}}'
`);

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  const event = {
    type: "before_agent_start",
    prompt: "hello",
    systemPrompt: "base prompt",
    systemPromptOptions: { cwd: fixture.workspace, sections: { existing: "keep" } },
  };
  await handlers.get("before_agent_start")(event, { cwd: fixture.workspace, signal: undefined });

  assert.equal(event.systemPromptOptions.sections.existing, "keep");
  assert.equal(event.systemPromptOptions.sections.dotfiles_repository_context, "repo reminder");
  assert.deepEqual(JSON.parse(readFileSync(inputLog, "utf8")), {
    hook_event_name: "SessionStart",
    cwd: fixture.workspace,
  });
});

test("completion sound runs only for non-aborted Pi agent_settled", async (t) => {
  const fixture = makeFixture(t);
  const notificationLog = join(fixture.root, "notification.log");
  fixture.setEnv("PI_NOTIFY_LOG", notificationLog);
  writeExecutable(join(fixture.hooks, "agent_turn_done_notify.sh"), `#!/bin/sh
printf 'done\\n' >> "$PI_NOTIFY_LOG"
`);

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  const handle = handlers.get("agent_settled");
  await handle({ type: "agent_settled", aborted: true }, { cwd: fixture.workspace });
  assert.equal(existsSync(notificationLog), false);
  await handle({ type: "agent_settled", aborted: false }, { cwd: fixture.workspace });
  assert.equal(readFileSync(notificationLog, "utf8"), "done\n");
});

test("successful Pi writes sync paired Python files through the shared Jupytext hook", async (t) => {
  const fixture = makeFixture(t);
  linkHook(fixture, "jupytext_sync.sh");
  const notebooks = join(fixture.workspace, "notebooks");
  mkdirSync(notebooks, { recursive: true });
  writeFileSync(join(notebooks, "model.py"), "# paired source\n");
  writeFileSync(join(notebooks, "model.ipynb"), "{}\n");
  const jupytextLog = join(fixture.root, "jupytext.args");
  fixture.setEnv("JUPYTEXT_LOG", jupytextLog);
  writeExecutable(join(fixture.bin, "jupytext"), `#!/bin/sh
printf '%s\\n' "$@" >> "$JUPYTEXT_LOG"
`);

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  await handlers.get("tool_result")(
    toolResult("write", { path: "notebooks/model.py", content: "# updated source\n" }),
    { cwd: fixture.workspace, signal: undefined },
  );

  assert.deepEqual(readFileSync(jupytextLog, "utf8").trim().split("\n"), [
    "--sync",
    join(notebooks, "model.py"),
  ]);
});

test("failed file tools do not run Jupytext or prose lint", async (t) => {
  const fixture = makeFixture(t);
  linkHook(fixture, "jupytext_sync.sh");
  linkHook(fixture, "japanese_prose_lint.sh");
  const notebookPath = join(fixture.workspace, "model.py");
  writeFileSync(notebookPath, "# source\n");
  writeFileSync(join(fixture.workspace, "model.ipynb"), "{}\n");
  const jupytextLog = join(fixture.root, "jupytext.args");
  fixture.setEnv("JUPYTEXT_LOG", jupytextLog);
  writeExecutable(join(fixture.bin, "jupytext"), `#!/bin/sh
printf '%s\\n' "$@" >> "$JUPYTEXT_LOG"
`);

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  const result = await handlers.get("tool_result")(
    toolResult("write", { path: "model.py", content: "SECRET_INPUT_SHOULD_NOT_RUN" }, { isError: true }),
    { cwd: fixture.workspace, signal: undefined },
  );

  assert.equal(result, undefined);
  assert.equal(existsSync(jupytextLog), false);
});

test("Pi prose feedback is appended without dropping tool content or structured data", async (t) => {
  const fixture = makeFixture(t);
  linkHook(fixture, "japanese_prose_lint.sh");
  const docs = join(fixture.workspace, "docs");
  mkdirSync(docs, { recursive: true });
  const document = join(docs, "report.md");
  writeFileSync(document, "シンプル。それだけ。それが本質です。\n\nそれだけ。それが本質だと考えます。\n");

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  const originalContent = [{ type: "text", text: "Successfully edited report.md" }];
  const structuredContent = { status: "written" };
  const result = await handlers.get("tool_result")(
    toolResult(
      "edit",
      {
        path: "docs/report.md",
        edits: [{ oldText: "古い表現", newText: "それだけ。それが本質だと考えます。" }],
      },
      { content: originalContent, structuredContent },
    ),
    { cwd: fixture.workspace, signal: undefined },
  );

  assert.deepEqual(result.content[0], originalContent[0]);
  assert.equal(result.content.length, 2);
  assert.match(result.content[1].text, /JP003/);
  assert.match(result.content[1].text, /report\.md:3:/);
  assert.doesNotMatch(result.content[1].text, /report\.md:1:/);
  assert.deepEqual(result.structuredContent, structuredContent);
  assert.equal("block" in result, false);
});

test("hook errors are visible without echoing stderr or the file input", async (t) => {
  const fixture = makeFixture(t);
  writeExecutable(join(fixture.hooks, "japanese_prose_lint.sh"), `#!/bin/sh
cat > /dev/null
printf '%s\\n' 'SENSITIVE_STDERR_MARKER' >&2
exit 17
`);
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.map(String).join(" "));
  t.after(() => {
    console.warn = originalWarn;
  });

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  const result = await handlers.get("tool_result")(
    toolResult("edit", {
      path: "report.md",
      edits: [{ oldText: "x", newText: "SECRET_INPUT_MARKER" }],
    }),
    { cwd: fixture.workspace, signal: undefined },
  );

  assert.equal(result, undefined);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /japanese_prose_lint\.sh/);
  assert.doesNotMatch(warnings[0], /SENSITIVE_STDERR_MARKER|SECRET_INPUT_MARKER/);
});

test("stuck context hooks are terminated at the configured timeout", async (t) => {
  if (process.platform === "win32") t.skip("Pi's shared shell hooks require a POSIX shell");
  const fixture = makeFixture(t);
  writeExecutable(join(fixture.hooks, "agent_context_reminder.sh"), `#!/bin/sh
sleep 30
`);
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.map(String).join(" "));
  t.after(() => {
    console.warn = originalWarn;
  });

  const { pi, handlers } = createExtensionHost();
  await (await loadExtension(dotfilesExtensionPath))(pi);
  const event = {
    type: "before_agent_start",
    prompt: "hello",
    systemPrompt: "base prompt",
    systemPromptOptions: { cwd: fixture.workspace, sections: {} },
  };
  const startedAt = Date.now();
  await handlers.get("before_agent_start")(event, { cwd: fixture.workspace, signal: undefined });

  assert.ok(Date.now() - startedAt < 10_000, "hook timeout did not return control promptly");
  assert.equal(event.systemPromptOptions.sections.dotfiles_repository_context, undefined);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /agent_context_reminder\.sh/);
  assert.match(warnings[0], /timed out/);
});
