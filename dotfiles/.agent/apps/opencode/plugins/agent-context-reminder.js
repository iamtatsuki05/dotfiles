import { spawn } from "node:child_process"

const runReminderHook = (cwd) => {
  const hookPath = `${process.env.HOME}/.config/opencode/hooks/agent_context_reminder.sh`
  const payload = JSON.stringify({
    hook_event_name: "SessionStart",
    cwd,
  })

  return new Promise((resolve) => {
    const child = spawn("zsh", [hookPath], {
      stdio: ["pipe", "pipe", "ignore"],
    })
    let stdout = ""

    child.stdout.setEncoding("utf8")
    child.stdout.on("data", (chunk) => {
      stdout += chunk
    })
    child.on("error", () => resolve(""))
    child.on("close", () => {
      try {
        const parsed = JSON.parse(stdout)
        resolve(
          parsed.context
            || parsed.additionalContext
            || parsed.hookSpecificOutput?.additionalContext
            || "",
        )
      } catch {
        resolve("")
      }
    })

    child.stdin.end(payload)
  })
}

export const AgentContextReminder = async ({ directory, worktree }) => {
  const cwd = worktree || directory || process.cwd()

  return {
    "experimental.session.compacting": async (_input, output) => {
      const context = await runReminderHook(cwd)
      if (!context) return

      if (Array.isArray(output.context)) {
        output.context.push(context)
      }
    },
  }
}
