import { extname } from "node:path";

const PROSE_EXTENSIONS = new Set([".md", ".markdown", ".txt"]);

export const RTK_GUIDANCE =
  "For shell commands supported by RTK, prefix the command with `rtk` to reduce output. Use `rtk proxy <command>` when full, unfiltered output is needed. DSH's built-in `read`, `write`, and `edit` tools do not run through RTK.";

export function makePostToolPayload(exec) {
  return {
    hook_event_name: "PostToolUse",
    cwd: exec.agent.session.header.cwd,
    tool_name: exec.name,
    tool_input: exec.arguments,
  };
}

export function postToolTargets(exec, result) {
  if (result?.isError !== false || !["write", "edit"].includes(exec?.name)) {
    return undefined;
  }

  const args = exec.arguments;
  if (typeof args?.file_path !== "string" || args.file_path.length === 0) {
    return undefined;
  }

  const extension = extname(args.file_path).toLowerCase();
  return {
    jupytext: extname(args.file_path) === ".py",
    prose: PROSE_EXTENSIONS.has(extension),
  };
}

export function proseContextFromOutput(output) {
  if (output?.exitCode !== 0 || typeof output.stdout !== "string") return undefined;
  try {
    const parsed = JSON.parse(output.stdout);
    return typeof parsed.additionalContext === "string" && parsed.additionalContext.trim()
      ? parsed.additionalContext.trim()
      : undefined;
  } catch {
    return undefined;
  }
}

export function isSuccessfulTurnEnd(event) {
  return event?.type === "turn/end" && event.data?.reason?.kind === "completed";
}
