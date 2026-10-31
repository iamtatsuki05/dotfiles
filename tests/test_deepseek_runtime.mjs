import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, mkdirSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const appDir = join(repoRoot, "dotfiles/.agent/apps/deepseek");
const installedDsh = requireInstalledDsh();

function requireInstalledDsh() {
  const located = spawnSync("mise", ["where", "npm:@deepseek-ai/dsh@0.2.0-rc.2"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  assert.equal(
    located.status,
    0,
    `native runtime probe requires mise npm:@deepseek-ai/dsh@0.2.0-rc.2: ${located.stderr || located.error?.message || "not installed"}`,
  );
  const install = located.stdout.trim();
  const bin = join(install, "bin/dsh");
  assert.ok(existsSync(bin), `DSH CLI must exist at ${bin}`);
  const peerScope = join(install, "lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai");
  assert.ok(existsSync(peerScope), `DSH peer package scope must exist at ${peerScope}`);
  return { install, bin, peerScope };
}

async function loadNativePlugin() {
  const temp = mkdtempSync(join(tmpdir(), "deepseek-plugin-fixture-"));
  const packageDir = join(temp, "plugin");
  cpSync(appDir, packageDir, { recursive: true });
  mkdirSync(join(packageDir, "node_modules"), { recursive: true });
  symlinkSync(installedDsh.peerScope, join(packageDir, "node_modules/@deepseek-ai"), "dir");
  try {
    const plugin = await import(`${pathToFileURL(join(packageDir, "index.js")).href}?fixture=${Date.now()}`);
    return { plugin, temp };
  } catch (error) {
    rmSync(temp, { recursive: true, force: true });
    throw new Error(`native DSH plugin must import through its installed peer packages: ${error.message}`, { cause: error });
  }
}

function fakeContext() {
  const handlers = new Map();
  const commands = [];
  const warnings = [];
  const promptSections = [];
  let dispose;
  const shell = {
    resolve(request) {
      return request;
    },
    async execute(request) {
      commands.push(request);
      const payload = JSON.parse(request.stdin ?? "{}");
      let stdout = "";
      if (request.command.includes("agent_context_reminder.sh")) {
        stdout = JSON.stringify({
          hookSpecificOutput: {
            hookEventName: payload.hook_event_name,
            additionalContext: "repository reminder",
          },
        });
      } else if (request.command.includes("japanese_prose_lint.sh")) {
        stdout = JSON.stringify({ additionalContext: "prose finding" });
      }
      return {
        result: async () => ({
          exitCode: 0,
          stdout: { text: stdout },
          stderr: { text: "" },
        }),
      };
    },
  };
  const ctx = {
    shell,
    systemPrompt: {
      getSectionOrder(name) {
        assert.equal(name, "TOOL_BASH");
        return 1000;
      },
      section(section) {
        promptSections.push(section);
        return () => {};
      },
    },
    effect(setup) {
      dispose = setup();
      return () => dispose?.();
    },
    on(name, listener) {
      handlers.set(name, listener);
    },
    logger: { warn: (message) => warnings.push(message) },
  };
  return { ctx, handlers, commands, warnings, promptSections, dispose: () => dispose?.() };
}

test("native lifecycle hooks inject context before the first step and preserve tool results", async () => {
  const loaded = await loadNativePlugin();
  const priorDshHome = process.env.DSH_HOME;
  const hookHome = mkdtempSync(join(tmpdir(), "deepseek-hook-home-"));
  process.env.DSH_HOME = hookHome;
  const fixture = fakeContext();
  const controller = new AbortController();
  const agent = { session: { header: { id: "session-fixture", cwd: "/workspace" } } };

  try {
    loaded.plugin.apply(fixture.ctx);
    const section = fixture.promptSections.find((item) => item.name === "dotfiles:deepseek-rtk-guidance");
    assert.ok(section);
    assert.match(section.text, /`rtk`/);
    assert.match(section.text, /`rtk proxy <command>`/);

    const preStep = fixture.handlers.get("agent/pre-step");
    const originalMessages = [];
    const first = await preStep(
      { agent, messages: originalMessages, turn: 1, step: 1, signal: controller.signal },
      async () => ({ kind: "enter", messages: originalMessages }),
    );
    assert.equal(first.kind, "enter");
    assert.equal(first.messages.length, 1);
    assert.equal(first.messages[0].content[0].text, "repository reminder");
    assert.equal(fixture.commands.length, 1);
    assert.equal(fixture.commands[0].workdir, "/workspace");

    const second = await preStep(
      { agent, messages: originalMessages, turn: 2, step: 1, signal: controller.signal },
      async () => ({ kind: "enter", messages: originalMessages }),
    );
    assert.equal(second.messages.length, 0);
    assert.equal(fixture.commands.length, 1, "the reminder is injected once per agent");

    const postTool = fixture.handlers.get("tools/post-execute");
    const structuredValue = { path: "notes.md", operation: "update" };
    const priorContext = { id: "prior", role: "user", content: [{ type: "text", text: "prior context" }] };
    const result = await postTool(
      {
        name: "edit",
        arguments: { file_path: "notes.md", old_string: "before", new_string: "after" },
        agent,
        signal: controller.signal,
      },
      { isError: false, value: structuredValue, content: [{ type: "text", text: "edited" }] },
      async () => ({ kind: "accept", value: structuredValue, additionalContexts: [priorContext] }),
    );
    assert.equal(result.kind, "accept");
    assert.equal(result.value, structuredValue);
    assert.equal(result.additionalContexts[0], priorContext);
    assert.equal(result.additionalContexts[1].content[0].text, "prose finding");
    const proseCall = fixture.commands.find((call) => call.command.includes("japanese_prose_lint.sh"));
    assert.ok(proseCall);
    assert.deepEqual(JSON.parse(proseCall.stdin), {
      hook_event_name: "PostToolUse",
      cwd: "/workspace",
      tool_name: "edit",
      tool_input: { file_path: "notes.md", old_string: "before", new_string: "after" },
    });

    const sessionEvent = fixture.handlers.get("session/event");
    const session = { header: { id: "session-fixture", cwd: "/workspace" } };
    sessionEvent(session, { type: "turn/end", data: { turn: 2, reason: { kind: "error" } } });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(fixture.commands.some((call) => call.command.includes("agent_turn_done_notify.sh")), false);

    sessionEvent(session, { type: "turn/end", data: { turn: 3, reason: { kind: "completed" } } });
    await fixture.dispose();
    assert.equal(fixture.commands.some((call) => call.command.includes("agent_turn_done_notify.sh")), true);
  } finally {
    await fixture.dispose();
    if (priorDshHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = priorDshHome;
    rmSync(hookHome, { recursive: true, force: true });
    rmSync(loaded.temp, { recursive: true, force: true });
  }
});

test("published DSH composes the local plugin and MCP patch without starting model or MCP services", () => {
  const temp = mkdtempSync(join(tmpdir(), "deepseek-dsh-fixture-"));
  const home = join(temp, "home");
  const dshHome = join(home, ".dsh");
  mkdirSync(join(dshHome, "hooks"), { recursive: true });
  const env = {
    ...process.env,
    HOME: home,
    DSH_HOME: dshHome,
    XDG_CONFIG_HOME: join(temp, "config"),
  };
  cpSync(join(repoRoot, "dotfiles/.agent/hooks"), join(dshHome, "hooks"), { recursive: true });
  cpSync(join(appDir, "cordis.patch.yml"), join(dshHome, "cordis.patch.yml"));

  try {
    const initialize = spawnSync(installedDsh.bin, [
      "--profile",
      "fixture",
      "--from-default-profile",
      "headless",
      "--dump-default-config",
    ], {
      cwd: temp,
      encoding: "utf8",
      env,
      timeout: 30_000,
    });
    assert.equal(initialize.status, 0, initialize.stderr || initialize.error?.message);
    const localPackageLink = join(dshHome, "profiles/fixture/node_modules/@dotfiles/deepseek-native");
    mkdirSync(dirname(localPackageLink), { recursive: true });
    symlinkSync(appDir, localPackageLink, "dir");

    const dump = spawnSync(installedDsh.bin, ["--profile", "fixture", "--dump-config"], {
      cwd: temp,
      encoding: "utf8",
      env,
      timeout: 30_000,
    });
    assert.equal(dump.status, 0, dump.stderr || dump.error?.message);
    for (const server of ["deepwiki", "devin", "figma", "notion"]) {
      assert.match(dump.stdout, new RegExp(`serverName: ${server}`));
    }
    assert.match(dump.stdout, /name: '@dotfiles\/deepseek-native'/);
    assert.match(dump.stdout, /profileContext/);
    assert.doesNotMatch(dump.stdout, /mcp-playwright/);

    const schema = spawnSync(installedDsh.bin, ["--profile", "fixture", "--dump-config-schema"], {
      cwd: temp,
      encoding: "utf8",
      env,
      timeout: 30_000,
    });
    assert.equal(schema.status, 0, schema.stderr || schema.error?.message);
    assert.match(schema.stdout, /@deepseek-ai\/dsh-mcp-client/);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
});
