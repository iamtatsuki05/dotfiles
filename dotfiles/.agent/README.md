# AI Agent Files

Japanese version: [README_JA.md](README_JA.md)

This directory is the source of truth for local AI CLI agents.

Internal tools that call Agent CLIs are tracked in [AGENT_SUPPORT.md](AGENT_SUPPORT.md). Update that matrix whenever adding or removing a supported Agent.

Managed agents:

- `codex`
- `claude-code`
- `copilot`
- `cursor-agent`
- `devin`
- `antigravity-cli`
- `hermes`
- `opencode`
- `openclaw`

The tools themselves are installed by `mise` where available. Antigravity CLI is managed as the Homebrew Cask `antigravity`, which provides the `agy` binary. The files here manage prompts, per-agent configuration, MCP servers, hooks, skills, and Waza eval suites.

## Layout

- `AGENTS.md`: shared prompt copied into each supported tool home. The repository root intentionally does not contain an `AGENTS.md` symlink.
- `apps/`: per-agent config files. See [apps/README.md](apps/README.md).
- `hooks/`: shared hook scripts such as `jupytext_sync.sh`, `agent_context_reminder.sh`, and `agent_turn_done_notify.sh`. See [hooks/README.md](hooks/README.md).
- `skills/`: shared skills used by Codex-compatible agents and Waza. See [skills/README.md](skills/README.md) for the hierarchy, origins, and per-skill summaries.
- `evals/`: Waza eval suites for skills. See [evals/README.md](evals/README.md).
- `pets/`: packaged Codex pet assets. See [pets/README.md](pets/README.md).
- `sync.sh`: wrapper around `scripts/setup_agent_files.sh`.
- `changes/`: local work notes for the current task. This is not user-facing documentation.

## Sync

Run this after editing files under `dotfiles/.agent/`:

```bash
zsh dotfiles/.agent/sync.sh
```

`sync.sh` delegates to `scripts/setup_agent_files.sh`. It creates symlinks into tool homes and generates agent-specific env files from `~/.config/shell/secrets.env`.

## Config Map

| Source | Destination |
|---|---|
| `AGENTS.md` | `~/.codex/AGENTS.md` |
| `AGENTS.md` | `~/.claude/CLAUDE.md` |
| `AGENTS.md` | `~/.copilot/copilot-instructions.md` |
| `AGENTS.md` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/rules/AGENTS.md` |
| `AGENTS.md` | `~/.cursor/AGENT.md` |
| `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `AGENTS.md` | `~/.hermes/AGENTS.md` |
| `AGENTS.md` | `~/.openclaw/workspace/AGENTS.md` |
| `apps/claude/settings.json` | `~/.claude/settings.json` |
| `apps/claude/.mcp.json` | `~/.claude/.mcp.json` |
| `apps/copilot/settings.json` | `~/.copilot/settings.json` |
| `apps/copilot/mcp-config.json` | `~/.copilot/mcp-config.json` |
| `apps/codex/config.toml` | `~/.codex/config.toml` |
| `apps/codex/hooks.json` | `~/.codex/hooks.json` |
| `apps/cursor/cli-config.json` | `~/.cursor/cli-config.json` |
| `apps/cursor/hooks.json` | `~/.cursor/hooks.json` |
| `apps/cursor/mcp.json` | `~/.cursor/mcp.json` |
| `apps/devin/config.json` | `~/.config/devin/config.json` |
| `apps/antigravity-cli/settings.json` | `~/.gemini/antigravity-cli/settings.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/plugin.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/plugin.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/mcp_config.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/mcp_config.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/hooks.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/hooks.json` |
| `apps/hermes-agent/config.yaml` | `~/.hermes/config.yaml` |
| `apps/opencode/opencode.json` | `~/.config/opencode/opencode.json` |
| `apps/opencode/plugins/` | `~/.config/opencode/plugins/` |
| `apps/openclaw/openclaw.json` | `~/.openclaw/openclaw.json` |

`skills/` is linked to each supported agent home. For Antigravity CLI, it is linked into `~/.gemini/antigravity-cli/plugins/dotfiles-agent/skills`. For OpenClaw, it is linked to `~/.openclaw/workspace/skills`. Shared hook scripts are linked to `~/.claude/hooks/`, `~/.codex/hooks/`, `~/.copilot/hooks/`, `~/.cursor/hooks/`, `~/.config/devin/hooks/`, `~/.gemini/antigravity-cli/hooks/`, `~/.config/opencode/hooks/`, and `~/.hermes/agent-hooks/`.

Hermes also links files from `apps/hermes-agent/agent-hooks/` into `~/.hermes/agent-hooks/`.

`agent_context_reminder.sh` injects the same repository reminder into supported session or prompt hook phases for Claude Code, Codex, Copilot, Cursor, Devin, Antigravity CLI, and Hermes. opencode loads the shared hook through a plugin for compaction context, because it exposes plugin events rather than Claude-style prompt hooks. OpenClaw enables its bundled `bootstrap-extra-files` internal hook to load the shared `AGENTS.md` from the managed workspace.

`agent_turn_done_notify.sh` is registered on compatible end-of-turn events for Claude Code, Copilot, Cursor, Devin, Antigravity CLI, Hermes, and opencode. For Claude Code, completion uses the `Stop` hook; `Notification` is only for permission or idle-input notifications. Codex keeps using its native `notify` setting and the same shared hook is still linked into `~/.codex/hooks/` for reuse.

## Ignore And Secrets

Project-level exclusions are split by agent capability:

- Cursor uses the repository root `.cursorignore`, which points to `apps/cursor/.cursorignore`.
- Copilot uses `.gitignore` through `respectGitignore`.
- Devin uses `respect_gitignore` plus explicit permission denies in `apps/devin/config.json`.
- Codex, Claude, Antigravity CLI, opencode, Cursor, Devin, and Hermes have their own ignore or permission rules in their app configs. OpenClaw is currently managed for workspace, skills, bootstrap hooks, and `mcp.servers`; file-level secret deny rules are not mirrored yet because its hook/policy surface is not directly compatible with the existing shared shell hook.

Secrets belong in `~/.config/shell/secrets.env`, not in this directory. `sync.sh` currently writes `DEVIN_API_KEY` into:

- `~/.gemini/antigravity-cli/.env`
- `~/.hermes/.env`

Waza model suites use the `copilot-sdk` executor, which requires `GITHUB_TOKEN`.

## Jupyter Notebooks

AI tools should edit paired `.py` files instead of `.ipynb` files. `hooks/jupytext_sync.sh` runs after supported file edits and syncs paired notebooks.

To pair a new notebook:

```bash
jupytext --set-formats ipynb,py:percent notebook.py
```

## Waza

Waza is included in the Nix CLI package set as `dotfiles.waza`.

Common commands:

```bash
mise run waza-check
mise run waza-eval
mise run waza-eval-all
mise run waza-eval-model -- --allow
mise run waza-eval-model -- --agent all --dry-run
mise run waza-dashboard
```

To run model eval tasks through one CLI agent:

```bash
mise run waza-eval-model -- --agent codex --allow
mise run waza-eval-model -- --agent claude --allow
mise run waza-eval-model -- --agent antigravity --allow
mise run waza-eval-model -- --agent copilot --allow
mise run waza-eval-model -- --agent devin --allow
mise run waza-eval-model -- --agent cursor --allow
mise run waza-eval-model -- --agent opencode --allow
mise run waza-eval-model -- --agent hermes --allow
mise run waza-eval-model -- --agent openclaw --allow
```

Use `--dry-run` to inspect suites without invoking an AI CLI. Results are written under `.waza-results/`.

## External Skill Upstreams

Vendored third-party skills are tracked in `skills/upstreams.json`. The manifest records the upstream GitHub repository, branch, pinned commit, local paths, and local tree hash.

Common commands:

```bash
python3 scripts/agent_skill_upstreams.py check
python3 scripts/agent_skill_upstreams.py updates
python3 scripts/agent_skill_upstreams.py update
mise run agent-skill-update
```

`update` defaults to every registered upstream at the latest branch head. It generates a review prompt, runs the selected Agent, writes review reports under `work/skill-upstream-reviews/`, and applies the update only when every report says `update recommendation: approve` without Critical or High findings.

```bash
python3 scripts/agent_skill_upstreams.py update --dry-run
python3 scripts/agent_skill_upstreams.py update --review-agent antigravity-cli
python3 scripts/agent_skill_upstreams.py update --review-agent claude-code
python3 scripts/agent_skill_upstreams.py update --id superpowers --commit <40-char-sha>
```

`codex` is the default review agent. Valid review agents are `codex`, `claude-code`, `antigravity-cli`, `copilot`, `cursor-agent`, `devin`, `hermes`, `opencode`, and `openclaw`. The default Japanese review prompt is `skills/review-prompts/skill-upstream-security.md`; pass `--review-prompt <path>` to use a different prompt template. Keep the report keys such as `update recommendation` in English because the updater parses them.

For manual review workflows, lower-level commands are still available:

```bash
python3 scripts/agent_skill_upstreams.py security-prompt \
  --id superpowers \
  --review-agent codex \
  --commit <40-char-sha>
```

```bash
python3 scripts/agent_skill_upstreams.py apply-update \
  --id superpowers \
  --commit <40-char-sha> \
  --review-agent codex \
  --review-report dotfiles/.agent/work/<review-report>.md \
  --security-reviewed
```

The update command refreshes the vendored files, pinned commit, local tree hash, and security review metadata in the manifest.
