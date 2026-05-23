export const AgentTurnDoneNotify = async ({ $ }) => {
  let running = false

  return {
    event: async ({ event }) => {
      if (event.type !== "session.idle" || running) return

      running = true
      try {
        await $`zsh ${process.env.HOME}/.config/opencode/hooks/agent_turn_done_notify.sh`
      } finally {
        running = false
      }
    },
  }
}
