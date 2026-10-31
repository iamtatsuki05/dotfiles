import { dshHomePath } from "@deepseek-ai/dsh-home-paths";
import z from "@deepseek-ai/schemastery";
import { createUserMessage } from "@deepseek-ai/dsh-llm";
import { createDetachedRuns, runHook } from "@deepseek-ai/dsh-hook-protocol";
import { performance } from "node:perf_hooks";
import {
  RTK_GUIDANCE,
  isSuccessfulTurnEnd,
  makePostToolPayload,
  postToolTargets,
  proseContextFromOutput,
} from "./logic.mjs";

export const name = "dotfiles-deepseek-native";
export const inject = ["shell", "systemPrompt"];
export const Config = z.object({});

const HOOK_TIMEOUT_MS = 10_000;

function quoteShell(value) {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

function contextMessage(text, source) {
  return createUserMessage({
    content: [{ type: "text", text }],
    source: { kind: source },
  });
}

async function executeHook(ctx, command, payload, { cwd, signal, expectedEventName, timeoutMs = HOOK_TIMEOUT_MS }) {
  const { output } = await runHook(
    ctx.shell,
    { command, timeoutSec: timeoutMs / 1000 },
    {
      payload,
      cwd,
      signal,
      trailingNewline: false,
      defaultTimeoutMs: timeoutMs,
      expectedEventName,
    },
    () => performance.now(),
  );
  return output;
}

function warnHookFailure(ctx, hookName, output) {
  if (output.exitCode !== 0) {
    ctx.logger.warn(`deepseek-native: ${hookName} hook exited with ${output.exitCode ?? "unknown"}`);
  }
}

export function apply(ctx) {
  const detached = createDetachedRuns();
  const remindedAgents = new WeakSet();
  const notifiedTurns = new WeakMap();

  ctx.effect(() => () => detached.drain(), "deepseek-native: drain completion notifications");
  ctx.systemPrompt.section({
    name: "dotfiles:deepseek-rtk-guidance",
    order: ctx.systemPrompt.getSectionOrder("TOOL_BASH") + 1,
    text: RTK_GUIDANCE,
  });

  ctx.on("agent/pre-step", async ({ agent, signal }, next) => {
    if (remindedAgents.has(agent)) return next();
    remindedAgents.add(agent);

    const cwd = agent.session.header.cwd;
    const eventName = "SessionStart";
    const output = await executeHook(
      ctx,
      quoteShell(dshHomePath("hooks", "agent_context_reminder.sh")),
      {
        session_id: agent.session.header.id,
        transcript_path: null,
        cwd,
        hook_event_name: eventName,
        model: "",
        permission_mode: "default",
      },
      { cwd, signal, expectedEventName: eventName },
    );

    if (output.exitCode !== 0) {
      warnHookFailure(ctx, "agent_context_reminder", output);
    }
    const context = output.exitCode === 0 ? output.additionalContext?.trim() : "";
    const decision = await next();
    if (!context || decision.kind !== "enter") return decision;
    return {
      ...decision,
      messages: [...decision.messages, contextMessage(context, "dotfiles-agent-context-reminder")],
    };
  });

  ctx.on("tools/post-execute", async (exec, result, next) => {
    const decision = await next();
    const targets = postToolTargets(exec, result);
    if (!targets) return decision;

    const cwd = exec.agent?.session.header.cwd;
    if (!cwd) return decision;

    const payload = makePostToolPayload(exec);
    if (targets.jupytext) {
      const output = await executeHook(
        ctx,
        quoteShell(dshHomePath("hooks", "jupytext_sync.sh")),
        payload,
        { cwd, signal: exec.signal, expectedEventName: "PostToolUse", timeoutMs: 30_000 },
      );
      warnHookFailure(ctx, "jupytext_sync", output);
    }

    if (!targets.prose) return decision;
    const lintOutput = await executeHook(
      ctx,
      `${quoteShell(dshHomePath("hooks", "japanese_prose_lint.sh"))} --hook-agent dsh`,
      payload,
      { cwd, signal: exec.signal, expectedEventName: "PostToolUse" },
    );
    if (lintOutput.exitCode !== 0) {
      warnHookFailure(ctx, "japanese_prose_lint", lintOutput);
      return decision;
    }
    const feedback = proseContextFromOutput(lintOutput);
    if (!feedback) return decision;
    return {
      ...decision,
      additionalContexts: [
        ...(decision.additionalContexts ?? []),
        contextMessage(feedback, "dotfiles-japanese-prose-lint"),
      ],
    };
  });

  ctx.on("session/event", (session, event) => {
    if (!isSuccessfulTurnEnd(event)) return;
    const previous = notifiedTurns.get(session) ?? 0;
    if (event.data.turn <= previous) return;
    notifiedTurns.set(session, event.data.turn);

    const cwd = session.header.cwd;
    const run = executeHook(
      ctx,
      quoteShell(dshHomePath("hooks", "agent_turn_done_notify.sh")),
      {
        session_id: session.header.id,
        transcript_path: null,
        cwd,
        hook_event_name: "Stop",
        turn_id: String(event.data.turn),
        stop_hook_active: false,
        last_assistant_message: null,
      },
      { cwd, signal: detached.signal, expectedEventName: "Stop" },
    ).then((output) => warnHookFailure(ctx, "agent_turn_done_notify", output));
    detached.track(run);
  });
}
