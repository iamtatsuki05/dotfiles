# AI Agent Files

Japanese version: [README_JA.md](README_JA.md)

This directory is the source of truth for local AI CLI agents.

Managed agents:

- `codex`
- `claude-code`
- `copilot`
- `cursor-agent`
- `devin`
- `gemini-cli`
- `hermes`
- `opencode`

The tools themselves are installed by `mise`. The files here manage prompts, per-agent configuration, MCP servers, hooks, skills, and Waza eval suites.

## Layout

- `AGENTS.md`: shared prompt copied into each supported tool home. The repository root intentionally does not contain an `AGENTS.md` symlink.
- `apps/`: per-agent config files.
- `hooks/`: shared hook scripts, currently `jupytext_sync.sh`.
- `skills/`: shared skills used by Codex-compatible agents and Waza.
- `evals/`: Waza eval suites for skills.
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
| `AGENTS.md` | `~/.gemini/GEMINI.md` |
| `AGENTS.md` | `~/.cursor/AGENT.md` |
| `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `AGENTS.md` | `~/.hermes/AGENTS.md` |
| `apps/claude/settings.json` | `~/.claude/settings.json` |
| `apps/claude/.mcp.json` | `~/.claude/.mcp.json` |
| `apps/copilot/settings.json` | `~/.copilot/settings.json` |
| `apps/copilot/mcp-config.json` | `~/.copilot/mcp-config.json` |
| `apps/codex/config.toml` | `~/.codex/config.toml` |
| `apps/codex/hooks.json` | `~/.codex/hooks.json` |
| `apps/cursor/cli-config.json` | `~/.cursor/cli-config.json` |
| `apps/cursor/mcp.json` | `~/.cursor/mcp.json` |
| `apps/devin/config.json` | `~/.config/devin/config.json` |
| `apps/gemini/settings.json` | `~/.gemini/settings.json` |
| `apps/gemini/ignore` | `~/.gemini/ignore` |
| `apps/hermes-agent/config.yaml` | `~/.hermes/config.yaml` |
| `apps/opencode/opencode.json` | `~/.config/opencode/opencode.json` |
| `apps/opencode/plugins/` | `~/.config/opencode/plugins/` |

`skills/` is linked to each supported agent home. Shared hook scripts are linked to `~/.claude/hooks/`, `~/.codex/hooks/`, `~/.copilot/hooks/`, `~/.config/devin/hooks/`, `~/.gemini/hooks/`, `~/.config/opencode/hooks/`, and `~/.hermes/agent-hooks/`.

Hermes also links files from `apps/hermes-agent/agent-hooks/` into `~/.hermes/agent-hooks/`.

## Ignore And Secrets

Project-level exclusions are split by agent capability:

- Cursor uses the repository root `.cursorignore`, which points to `apps/cursor/.cursorignore`.
- Copilot uses `.gitignore` through `respectGitignore`.
- Devin uses `respect_gitignore` plus explicit permission denies in `apps/devin/config.json`.
- Codex, Claude, Gemini, opencode, and Hermes have their own ignore or permission rules in their app configs.

Secrets belong in `~/.config/shell/secrets.env`, not in this directory. `sync.sh` currently writes `DEVIN_API_KEY` into:

- `~/.gemini/.env`
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
mise run waza-eval-cli-agents -- --dry-run
mise run waza-dashboard
```

To run model eval tasks through one CLI agent:

```bash
mise run waza-eval-codex -- --allow
mise run waza-eval-claude -- --allow
mise run waza-eval-gemini -- --allow
mise run waza-eval-copilot -- --allow
mise run waza-eval-devin -- --allow
mise run waza-eval-cursor -- --allow
mise run waza-eval-opencode -- --allow
mise run waza-eval-hermes -- --allow
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

`update` defaults to every registered upstream at the latest branch head. It generates a review prompt, runs the selected Agent, writes review reports under `changes/skill-upstream-reviews/`, and applies the update only when every report says `update recommendation: approve` without Critical or High findings.

```bash
python3 scripts/agent_skill_upstreams.py update --dry-run
python3 scripts/agent_skill_upstreams.py update --review-agent gemini-cli
python3 scripts/agent_skill_upstreams.py update --id superpowers --commit <40-char-sha>
```

`codex` is the default review agent. Valid review agents are `codex`, `claude-code`, `copilot`, `cursor-agent`, `devin`, `gemini-cli`, `hermes`, and `opencode`. The default Japanese review prompt is `skills/review-prompts/skill-upstream-security.md`; pass `--review-prompt <path>` to use a different prompt template. Keep the report keys such as `update recommendation` in English because the updater parses them.

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
  --review-report dotfiles/.agent/changes/<review-report>.md \
  --security-reviewed
```

The update command refreshes the vendored files, pinned commit, local tree hash, and security review metadata in the manifest.
