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
- `grok`

The tools themselves are installed by `mise` where available. Herdr is also installed by `mise`, but it is treated as a terminal multiplexer / agent runtime rather than a canonical agent. Antigravity CLI is managed as the Homebrew Cask `antigravity`, which provides the `agy` binary. The files here manage prompts, per-agent configuration, MCP servers, hooks, skills, and Waza eval suites.

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

## Herdr

Herdr itself is installed by `mise` via `github:ogulcancelik/herdr`. The official Herdr skill is vendored under `skills/herdr/` with the upstream license and a local safety overlay.

### Start an opt-in Orca agent team without changing normal CLI behavior

See the dedicated [Agent Team guide](apps/agent-team/README.md) for the quick
start, architecture, configuration reference, and troubleshooting steps.

`agent-team` creates a project-specific Orca Run; Orca is its only orchestration backend. It starts only the user-facing Main agent initially. Main creates Orca Tasks for Planner, Worker, and Reviewer on demand and launches each role in a dedicated terminal as a supervised Dispatch. Role providers, models, efforts, prompts, permissions, and transports live in `apps/agent-team/config.toml` and `apps/agent-team/prompts/`. Plain `claude` and `codex` invocations keep their existing behavior because the launcher passes role overrides only to the processes it starts.

Config version 3 requires every role to explicitly set `transport = "direct"` or `transport = "acp"`; missing or unsupported values fail fast. The canonical team is:

| Role | Provider / transport | Model / effort | Permission |
|---|---|---|---|
| Main | Claude / `direct` | `fable` / `high` | `orchestrator` |
| Planner | Claude / `acp` | `fable` / `high` | `read-only` |
| Worker | Codex / `direct` | `gpt-5.6-sol` / `medium` | `workspace-write` |
| Reviewer | Codex / `direct` | `gpt-5.6-sol` / `high` | `read-only` |

Initial ACP support is limited to Claude read-only background roles. Main ACP, Codex ACP, and workspace-write ACP fail fast because a compatibility probe found that Codex ACP `deny-all`/`read-only` settings did not block writes by Codex internal tools. ACP permission mediation is not a provider or OS sandbox; write roles therefore retain direct Codex permissions.

Main coordinates roles through the Run-scoped `agent_team` MCP server. It exposes only task creation, launch, wait, read, release, and question-reply operations for the three fixed background roles. It invokes Orca with argument arrays instead of a shell. Claude Main has no Bash tool; Planner's ACP invocation uses a `Read,Grep,Glob` allowlist. Reviewer runs as direct Codex with its built-in `:read-only` permission profile.

ACP uses the exact pins `acpx@0.13.2` and `@agentclientprotocol/claude-agent-acp@0.70.0`. The first run may make `npx` download packages over the network; no global install is used. Claude ACP uses the ambient `claude.ai` login and does not pass API credentials to the child. The subscription billing ledger itself has not been verified.

Each launched Codex role uses an isolated `CODEX_HOME` under the team runtime state directory. The launcher links only the normal home's `auth.json` and, when present, `AGENTS.md` and `skills`; it does not inherit the normal `config.toml`, hooks, plugins, or MCP servers. This keeps both ordinary Codex behavior and the team's tool surface separate.

The launcher marks the target workspace's Git root, or the workspace itself outside Git, as `untrusted` only inside each isolated Codex process. This suppresses Codex's interactive directory-trust onboarding while keeping project-local `.codex` config, hooks, and execution policies disabled. It does not change the normal Codex trust setting.

```bash
# Inspect role metadata and direct argv only; ACP argv is built when a role starts.
agent-team start --dry-run

# Register the repository with Orca once.
orca repo add --path "$PWD"

# Start the team and focus Main in Orca.
agent-team start

agent-team status
agent-team stop
```

After Main has started a Worker Dispatch, focus it with:

```bash
# Run only when Main has already started the Worker Dispatch.
agent-team attach worker
```

The existing `start`, `status`, `attach`, and `stop` commands remain unchanged. `start` invokes the configured external agent CLIs and can consume provider quota. It does not commit, push, publish, install integrations, or modify normal Claude/Codex config. Runtime state lives under `$XDG_STATE_HOME/agent-team/` (default `~/.local/state/agent-team/`). If the derived team state already exists, `start` fails and requires an explicit `attach` or `stop` command. `stop` targets only terminals owned by this team; the Orca Run remains as an audit record.

In config version 3, `workspace-write` roles require the Codex provider and use direct transport. For each process, the launcher derives a permission profile from Codex's built-in `:workspace` or `:read-only` profile and allows only the active Orca runtime socket; it allows no external domains. Workspace writes remain limited to normal workspace files while `.git/` and `.codex/` stay protected. Claude remains supported for Main and read-only roles; the launcher rejects a write-enabled Claude role instead of silently weakening isolation. To upgrade a team started by version-2 code, stop it first with the old code's `agent-team stop`, then switch to version 3. There is no legacy fallback.

Herdr integration installers mutate each agent's config home directly. Because `sync.sh` symlinks those homes back into this repository, do not run the installers against the live homes unless you intentionally want tracked config files to change. Generate into a scratch home first, inspect the diff, and then model the required generated files in this repository:

```bash
scratch_home="$(mktemp -d)"
mkdir -p "$scratch_home/codex"
CODEX_HOME="$scratch_home/codex" herdr integration install codex
find "$scratch_home" -maxdepth 3 -type f -print
```

Use the same scratch-home pattern with the target agent's documented config-home variable for other integrations (`claude`, `copilot`, `devin`, `opencode`, `hermes`, `cursor`). Only after reviewing the generated files should you copy the intended changes into `dotfiles/.agent/apps/*` and re-run `zsh dotfiles/.agent/sync.sh`.

## Config Map

| Source | Destination / use |
|---|---|
| `AGENTS.md` | `~/.codex/AGENTS.md` |
| `AGENTS.md` | `~/.claude/CLAUDE.md` |
| `AGENTS.md` | `~/.copilot/copilot-instructions.md` |
| `AGENTS.md` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/rules/AGENTS.md` |
| `AGENTS.md` | `~/.cursor/AGENT.md` |
| `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `AGENTS.md` | `~/.hermes/AGENTS.md` |
| `AGENTS.md` | `~/.openclaw/workspace/AGENTS.md` |
| `AGENTS.md` | `~/.grok/AGENTS.md` |
| `apps/claude/settings.json` | `~/.claude/settings.json` |
| `apps/claude/.mcp.json` | `~/.claude/.mcp.json` |
| `apps/copilot/settings.json` | `~/.copilot/settings.json` |
| `apps/copilot/mcp-config.json` | `~/.copilot/mcp-config.json` |
| `apps/codex/config.toml` | `~/.codex/config.toml` |
| `apps/codex/hooks.json` | `~/.codex/hooks.json` |
| `../../scripts/agent-team/agent-team` | `~/.local/bin/agent-team` |
| `../../scripts/agent-team/agent_team/mcp_server.py` | Launched through `agent-team _mcp-server` by the same package entrypoint |
| `../../scripts/agent-team/agent_team/runtime.py` | Imported by the package; no separate managed runtime link |
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
| `apps/grok/config.toml` | `~/.grok/config.toml` |

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
mise run waza-eval-model -- --agent grok --allow
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

`codex` is the default review agent. Valid review agents are `codex`, `claude-code`, `antigravity-cli`, `copilot`, `cursor-agent`, `devin`, `hermes`, `opencode`, `openclaw`, and `grok`. The default Japanese review prompt is `skills/review-prompts/skill-upstream-security.md`; pass `--review-prompt <path>` to use a different prompt template. Keep the report keys such as `update recommendation` in English because the updater parses them.

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
