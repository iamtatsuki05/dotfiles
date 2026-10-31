import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { extname, isAbsolute, join, resolve } from "node:path";
import type {
  AgentSettledEvent,
  BeforeAgentStartEvent,
  ExtensionAPI,
  ExtensionContext,
  ToolResultEvent,
  ToolResultEventResult,
} from "@earendil-works/pi-coding-agent";

const CONTEXT_SECTION = "dotfiles_repository_context";
const CONTEXT_TIMEOUT_MS = 5_000;
const NOTIFY_TIMEOUT_MS = 10_000;
const JUPYTEXT_TIMEOUT_MS = 30_000;
const PROSE_LINT_TIMEOUT_MS = 30_000;

type HookResult =
  | { ok: true; stdout: string }
  | { ok: false; reason: string };

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function getAgentDir(): string {
  const configured = process.env.PI_CODING_AGENT_DIR;
  if (!configured) return join(homedir(), ".pi", "agent");
  if (configured === "~") return homedir();
  if (configured.startsWith("~/")) return join(homedir(), configured.slice(2));
  return resolve(configured);
}

function getHookPath(name: string): string {
  return join(getAgentDir(), "hooks", name);
}

function signalProcessGroup(child: ReturnType<typeof spawn>, signal: NodeJS.Signals): void {
  if (child.pid === undefined) return;
  if (process.platform === "win32") {
    child.kill(signal);
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch {
    child.kill(signal);
  }
}

function runHook(
  name: string,
  payload: unknown,
  cwd: string,
  timeoutMs: number,
  args: string[] = [],
): Promise<HookResult> {
  const script = getHookPath(name);
  let input: string | undefined;
  try {
    input = payload === undefined ? undefined : JSON.stringify(payload);
  } catch {
    return Promise.resolve({ ok: false, reason: "invalid input" });
  }

  return new Promise((resolveResult) => {
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(script, args, {
        cwd,
        detached: process.platform !== "win32",
        shell: false,
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      resolveResult({ ok: false, reason: "could not start" });
      return;
    }

    let stdout = "";
    let timedOut = false;
    let settled = false;
    let timeout: NodeJS.Timeout | undefined;
    let escalation: NodeJS.Timeout | undefined;

    const finish = (result: HookResult) => {
      if (settled) return;
      settled = true;
      if (timeout) clearTimeout(timeout);
      if (escalation) clearTimeout(escalation);
      resolveResult(result);
    };

    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stdin?.on("error", () => {});
    child.on("error", () => finish({ ok: false, reason: "could not start" }));
    child.on("close", (code, signal) => {
      if (timedOut) {
        finish({ ok: false, reason: "timed out" });
      } else if (code === 0) {
        finish({ ok: true, stdout });
      } else {
        finish({ ok: false, reason: `exited (${code ?? signal ?? "unknown"})` });
      }
    });

    timeout = setTimeout(() => {
      timedOut = true;
      signalProcessGroup(child, "SIGTERM");
      escalation = setTimeout(() => {
        signalProcessGroup(child, "SIGKILL");
        finish({ ok: false, reason: "timed out" });
      }, 250);
    }, timeoutMs);

    child.stdin?.end(input);
  });
}

function warnHookFailure(name: string, result: Extract<HookResult, { ok: false }>): void {
  console.warn(`[dotfiles/pi] ${name} ${result.reason}`);
}

function parseHookJson(name: string, stdout: string): JsonRecord | undefined {
  if (!stdout.trim()) return undefined;
  try {
    const parsed: unknown = JSON.parse(stdout);
    if (isRecord(parsed)) return parsed;
  } catch {
  }
  console.warn(`[dotfiles/pi] ${name} returned invalid JSON`);
  return undefined;
}

function resolveToolPath(value: unknown, cwd: string): string | undefined {
  if (typeof value !== "string" || value.length === 0) return undefined;
  const expanded = value === "~" ? homedir() : value.startsWith("~/") ? join(homedir(), value.slice(2)) : value;
  return isAbsolute(expanded) ? expanded : resolve(cwd, expanded);
}

async function addRepositoryContext(
  event: BeforeAgentStartEvent,
  ctx: ExtensionContext,
): Promise<void> {
  const payload = { hook_event_name: "SessionStart", cwd: ctx.cwd };
  const result = await runHook("agent_context_reminder.sh", payload, ctx.cwd, CONTEXT_TIMEOUT_MS);
  if (!result.ok) {
    warnHookFailure("agent_context_reminder.sh", result);
    return;
  }

  const output = parseHookJson("agent_context_reminder.sh", result.stdout);
  const hookOutput = output?.hookSpecificOutput;
  const reminder = isRecord(hookOutput) && hookOutput.hookEventName === "SessionStart"
    ? hookOutput.additionalContext
    : undefined;
  if (typeof reminder !== "string" || reminder.length === 0) return;
  event.systemPromptOptions.sections[CONTEXT_SECTION] = reminder;
}

async function notifySettledAgent(event: AgentSettledEvent, ctx: ExtensionContext): Promise<void> {
  if (event.aborted) return;
  const result = await runHook("agent_turn_done_notify.sh", undefined, ctx.cwd, NOTIFY_TIMEOUT_MS);
  if (!result.ok) warnHookFailure("agent_turn_done_notify.sh", result);
}

function makeProseLintPayload(event: ToolResultEvent, path: string, cwd: string): JsonRecord | undefined {
  const input = isRecord(event.input) ? event.input : undefined;
  if (!input) return undefined;

  if (event.toolName === "write") {
    return {
      type: "tool_result",
      toolName: "write",
      input: { path },
      isError: false,
      cwd,
    };
  }

  if (event.toolName === "edit" && Array.isArray(input.edits)) {
    const edits = input.edits
      .filter(isRecord)
      .filter((edit) => typeof edit.newText === "string")
      .map((edit) => ({ newText: edit.newText }));
    if (edits.length === 0) return undefined;
    return {
      type: "tool_result",
      toolName: "edit",
      input: { path, edits },
      isError: false,
      cwd,
    };
  }
  return undefined;
}

async function runJupytextSync(path: string, cwd: string): Promise<void> {
  const payload = { tool_input: { file_path: path } };
  const result = await runHook("jupytext_sync.sh", payload, cwd, JUPYTEXT_TIMEOUT_MS);
  if (!result.ok) warnHookFailure("jupytext_sync.sh", result);
}

async function lintChangedProse(
  event: ToolResultEvent,
  path: string,
  ctx: ExtensionContext,
): Promise<ToolResultEventResult | undefined> {
  const payload = makeProseLintPayload(event, path, ctx.cwd);
  if (!payload) {
    if (event.toolName === "edit") {
      console.warn("[dotfiles/pi] skipped japanese prose lint for edit without newText entries");
    }
    return undefined;
  }

  const result = await runHook(
    "japanese_prose_lint.sh",
    payload,
    ctx.cwd,
    PROSE_LINT_TIMEOUT_MS,
    ["--hook-agent", "pi"],
  );
  if (!result.ok) {
    warnHookFailure("japanese_prose_lint.sh", result);
    return undefined;
  }

  const output = parseHookJson("japanese_prose_lint.sh", result.stdout);
  const additionalContext = output?.additionalContext;
  if (typeof additionalContext !== "string" || additionalContext.length === 0) return undefined;

  return {
    content: [...event.content, { type: "text", text: additionalContext }],
    ...(event.structuredContent === undefined ? {} : { structuredContent: event.structuredContent }),
  };
}

async function handleToolResult(event: ToolResultEvent, ctx: ExtensionContext): Promise<ToolResultEventResult | undefined> {
  if (event.isError || (event.toolName !== "write" && event.toolName !== "edit")) return undefined;
  const input = isRecord(event.input) ? event.input : undefined;
  const path = resolveToolPath(input?.path, ctx.cwd);
  if (!path) return undefined;

  if (extname(path).toLowerCase() === ".py") {
    await runJupytextSync(path, ctx.cwd);
    return undefined;
  }

  return lintChangedProse(event, path, ctx);
}

export default function (pi: ExtensionAPI): void {
  pi.on("before_agent_start", addRepositoryContext);
  pi.on("agent_settled", notifySettledAgent);
  pi.on("tool_result", handleToolResult);
}
