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
- `deepseek` (`dsh`)
- `pi`

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

## DeepSeek Harness / Pi

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) uses the canonical ID `deepseek` and the `dsh` binary. [Pi](https://github.com/earendil-works/pi) uses `pi`. Native integrations are verified with DSH `0.2.0-rc.2`, Pi `1.1.0`, and RTK `0.51.0`; both mise definitions pin these versions and use Node.js 24.

```sh
mise install npm:@deepseek-ai/dsh npm:@earendil-works/pi-coding-agent
zsh scripts/setup_agent_files.sh --only deepseek
zsh scripts/setup_agent_files.sh --only pi
```

`--only` updates one new agent home without syncing the other agents. Omit it for the standard all-agent sync. Add `--dry-run` to preview. New integration file conflicts fail before deployment. Pi settings are merged as regular files: managed model defaults and named MCP servers are updated, while fields such as `deviceId`, unrelated servers, and other extensions are preserved. Pi defaults to `openai/gpt-6-luna` with high reasoning; use `/login openai` and choose **Sign in with ChatGPT**. Credentials remain in client-owned files outside this repository.

Sync links the shared instructions to `~/.dsh/AGENTS.md` and `~/.pi/agent/AGENTS.md`. DeepSeek only discovers direct skill children, so it gets individual `dotfiles-<relative-path>` links under `~/.dsh/skills` (`/` becomes `--`); hidden directories such as `.system` are excluded. Only stale links into the current repository are removed. Pi recursively loads `~/.pi/agent/skills`; foreign links, files, and nonempty directories at that path are rejected.

Both native plugins add repository context, completion notifications, paired `.py` notebook synchronization, and advisory Japanese prose lint after successful built-in `write`/`edit` calls. Shell/custom-tool writes are outside the edit hook contract. Pi uses the unchanged official RTK extension to rewrite Bash commands. DeepSeek receives RTK usage guidance because its public tool API forbids argument rewriting; usage depends on the model following the instruction. No permission or sandbox override is added.

MCP definitions match the existing Codex set: DeepWiki, Devin, Figma, and Notion; Playwright remains disabled (absent from the DSH patch). Devin reads `DEVIN_API_KEY` from the process environment. MCP authorization is client-specific; configuration alone does not copy another client's OAuth grants. Pi supports `pi mcp login <server>`. The pinned DSH MCP client does not provide an OAuth login flow for Figma/Notion.

DeepSeek's `web`/`headless` profile settings and credential store remain client-owned. Its built-in `llm-pi-ai/openai-codex` authorization flow supports ChatGPT subscription login through the public authorization service, although this release's Models form only exposes API-key entry. After native OAuth, use this selection in each profile's `cordis.patch.yml`:

```yaml
- id: llm-pi-ai
  config:
    providers:
      openai-codex:
        transport: sse
- id: agent-default-model
  config:
    provider: openai-codex
    model: gpt-6-luna
    reasoningEffort: high
```

`transport: sse` avoids the pinned library's post-answer WebSocket cache delay ([upstream report](https://github.com/deepseek-ai/deepseek-harness/discussions/4190)) while retaining the same provider, model, credentials, and native tool loop. The home integration patch applies only to `web` and `headless`; sync links the local plugin package into those two profiles. Restart running clients after updating plugins.

Run `dsh headless -- "task"` or `pi --print --no-session -- "task"`. Waza (`--agent deepseek` / `--agent pi`, including `all`), upstream skill review (`--review-agent`), and `agent-job-scheduler` accept both IDs. DeepSeek is a developer preview, and both agents execute tools with the launching user's access: see [DeepSeek safety](https://github.com/deepseek-ai/deepseek-harness/blob/master/SAFETY.md) and [Pi security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md).

## Herdr

Herdr itself is installed by `mise` via `github:ogulcancelik/herdr`. The official Herdr skill is vendored under `skills/herdr/` with the upstream license and a local safety overlay.

Herdr integration installers mutate each agent's config home directly. Because `sync.sh` symlinks those homes back into this repository, do not run the installers against the live homes unless you intentionally want tracked config files to change. Generate into a scratch home first, inspect the diff, and then model the required generated files in this repository:

```bash
scratch_home="$(mktemp -d)"
mkdir -p "$scratch_home/codex"
CODEX_HOME="$scratch_home/codex" herdr integration install codex
find "$scratch_home" -maxdepth 3 -type f -print
```

Use the same scratch-home pattern with the target agent's documented config-home variable for other integrations (`claude`, `copilot`, `devin`, `opencode`, `hermes`, `cursor`). Only after reviewing the generated files should you copy the intended changes into `dotfiles/.agent/apps/*` and re-run `zsh dotfiles/.agent/sync.sh`.

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
| `AGENTS.md` | `~/.grok/AGENTS.md` |
| `AGENTS.md` | `~/.dsh/AGENTS.md` |
| `AGENTS.md` | `~/.pi/agent/AGENTS.md` |
| `skills/` | `~/.pi/agent/skills` |
| Parent directories of `skills/**/SKILL.md` | `~/.dsh/skills/dotfiles-<relative-path>` |
| `../../scripts/agent-run-compact` | `~/.local/bin/agent-run-compact` |
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
| `apps/grok/config.toml` | `~/.grok/config.toml` |

`skills/` is linked to each supported agent home. For Antigravity CLI, it is linked into `~/.gemini/antigravity-cli/plugins/dotfiles-agent/skills`. For OpenClaw, it is linked to `~/.openclaw/workspace/skills`. Hermes also uses a separate shared-skill path: `~/.hermes/skills` stays a real directory that Hermes owns, so its bundled-skill sync, `.bundled_manifest`, hub, and curator writes land there, and the shared tree is linked to `~/.hermes/dotfiles-skills` and loaded through `skills.external_dirs` in the managed `config.yaml`. Hermes treats that external tree as externally owned, but a user-directed `skill_manage` edit still writes to the repository. Shared hook scripts are linked to `~/.claude/hooks/`, `~/.codex/hooks/`, `~/.copilot/hooks/`, `~/.cursor/hooks/`, `~/.config/devin/hooks/`, `~/.gemini/antigravity-cli/hooks/`, `~/.config/opencode/hooks/`, and `~/.hermes/agent-hooks/`.

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
mise run waza-eval-model -- --agent deepseek --allow
mise run waza-eval-model -- --agent pi --allow
```

Use `--dry-run` to inspect suites without invoking an AI CLI. Results are written under `.waza-results/`.

## External Skill Upstreams

Vendored third-party skills are tracked in `skills/upstreams.json`. The manifest records the upstream GitHub repository (or public gist), branch, pinned commit, local paths, and local tree hash.

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

`codex` is the default review agent. Valid review agents are `codex`, `claude-code`, `antigravity-cli`, `copilot`, `cursor-agent`, `devin`, `hermes`, `opencode`, `openclaw`, `grok`, `deepseek`, and `pi`. The default Japanese review prompt is `skills/review-prompts/skill-upstream-security.md`; pass `--review-prompt <path>` to use a different prompt template. Keep the report keys such as `update recommendation` in English because the updater parses them.

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
