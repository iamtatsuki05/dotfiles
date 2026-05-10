export const JupytextSync = async ({ $ }) => {
  let running = false

  return {
    event: async ({ event }) => {
      if (event.type !== "file.edited" || running) return

      running = true
      try {
        await $`zsh ${process.env.HOME}/.config/opencode/hooks/jupytext_sync.sh`
      } finally {
        running = false
      }
    },
  }
}
