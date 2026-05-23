# Shared Hooks

Japanese version: [README_JA.md](README_JA.md)

This directory contains shared hook scripts used by multiple local AI agents.
`dotfiles/.agent/sync.sh` links these scripts into agent-specific hook locations.

## Hooks

| Hook | Purpose |
|---|---|
| `agent_context_reminder.sh` | Emits repository-specific reminder context for supported agent prompt or session hook phases. |
| `agent_turn_done_notify.sh` | Plays the shared completion sound for agents that support end-of-turn notifications. |
| `jupytext_sync.sh` | Keeps paired Jupyter notebooks synchronized after agents edit paired `.py` files. |

Agent-specific hook registration lives under `../apps/`.
Some agents use JSON hook maps, while others consume shell scripts from hook directories.

## Update Rules

- Keep hook behavior tool-agnostic when it is shared by multiple agents.
- Do not put secrets in hook scripts.
- Validate shell syntax after edits.
- When changing hook outputs, check both the script and the agent config that invokes it.
- If a hook parses JSON from an agent, test a representative payload with `python3 -m json.tool`.

## Common Checks

```bash
bash -n dotfiles/.agent/hooks/agent_context_reminder.sh
bash -n dotfiles/.agent/hooks/agent_turn_done_notify.sh
bash -n dotfiles/.agent/hooks/jupytext_sync.sh
printf '{}' | dotfiles/.agent/hooks/agent_context_reminder.sh | python3 -m json.tool
zsh tests/test_agent_sync.sh
```
