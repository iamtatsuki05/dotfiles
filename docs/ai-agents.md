# AI agent configuration

[日本語](ai-agents_JA.md) · [Documentation index](README.md)

Shared AI agent files are maintained under `dotfiles/.agent/`. Keep changes in
that canonical tree, run the sync, and validate both the managed source and
representative deployed targets.

## RTK command output compression

The standard setup installs [RTK](https://github.com/rtk-ai/rtk) through mise.
Agent sync enables its official hooks/plugins for Claude Code, Codex, Copilot,
Cursor, Antigravity CLI, Hermes, OpenCode, OpenClaw, and Pi. Devin and Grok have no
official RTK integration in the pinned release. DeepSeek Harness receives RTK usage guidance rather than automatic rewriting.

```sh
mise install github:rtk-ai/rtk
zsh scripts/setup_agent_files.sh
rtk --version
rtk gain
```

RTK rewrites supported shell commands; built-in file/search tools are outside
that scope. Copilot's native CLI hook protocol can deny with an RTK suggestion;
the PascalCase protocol supports `updatedInput`. In Codex, review and trust
`rtk hook codex` through `/hooks`; [Codex skips untrusted hooks](https://learn.chatgpt.com/docs/hooks).
Reload the agent after sync, including the OpenClaw Gateway.
Existing running sessions are not proof that the new hook has loaded. Use
`rtk proxy <command>` when full, unfiltered output is needed.

Permission checks may evaluate the rewritten `rtk ...` command, so command-specific
allow/deny rules can behave differently. This setup does not relax permissions.

The binary and official plugins are pinned to RTK release 0.51.0. See
[provenance and update instructions](../dotfiles/.agent/apps/rtk/README.md).
Do not run `rtk init` against live managed homes: it may modify the shared prompt
or replace symlinks. Agent sync deploys the reviewed integration instead.
An existing nonempty, unmanaged `opencode/plugins` directory is preserved and
skipped by sync; review that warning before expecting RTK to load there.

## TokenEconomizer for repeated agent work

The full setup registers [TokenEconomizer](https://github.com/microsoft/TokenEconomizer) after installing mise tools.
It targets Claude Code, Codex, Copilot CLI, Pi, and OpenCode. Upstream OpenCode support is experimental.
To install or reapply it in an existing environment:

```sh
mise run tokeneconomizer-setup -- --dry-run
mise run tokeneconomizer-setup
# Install for one agent only
bash scripts/setup_tokeneconomizer.sh --agent pi
```

Standalone agent sync and `main.sh --only agent` only apply configuration.
Run the installation command above on first use. Missing CLIs are reported as `SKIP`;
an explicitly selected `--agent` must be installed or setup fails.

Upstream commit `e428fc91b0f8ffecb5a596d56b14fe2f1485a5f9` is kept at
`~/.local/share/dotfiles/tokeneconomizer` without code changes.
The checkout retains its Apache-2.0 LICENSE and attribution.
Claude Code and Copilot register a local marketplace; Pi registers a local package.
Codex uses a Git marketplace pinned to the same commit, and OpenCode loads through its `plugin` setting.
Clients own their installation registries and Pi's package list. The shared prompt is unchanged.
Claude Code is installed in its default home and existing registered `claude-account` profiles.
An explicit `CLAUDE_CONFIG_DIR` limits installation to that home. Credentials are unchanged.
To update, change the commit in the setup script and Codex configuration together, validate, and rerun setup.
A checkout with local changes is rejected rather than overwritten.

Upstream defaults are retained: Micro-skills is off, unused-tool pruning and the loop guard are on.
Loop-guard judgments make additional model calls through the current agent.
Learned state lives in `~/.tokeneconomizer`; pruning changes project-local settings.
Pi and OpenCode do not prune tools. Existing RTK integrations remain in place.

Restart each agent after installation. In Codex, review and trust TokenEconomizer's definitions
through `/hooks`, then restart again. Setup does not change hook trust or permissions.
For Claude Code and Copilot, capture the tool catalog once and again after CLI updates.
These upstream catalog probes do not require a paid model response.

```sh
~/.local/share/dotfiles/tokeneconomizer/plugin/scripts/te --host claude toolprune refresh-catalog
~/.local/share/dotfiles/tokeneconomizer/plugin/scripts/te --host copilot toolprune refresh-catalog
~/.local/share/dotfiles/tokeneconomizer/plugin/scripts/te --host codex status
```

Set `TE_DISABLE=1` when starting an agent to pause the plugin. Before uninstalling,
run `te --host <agent> toolprune restore` in each project where tools were pruned,
then uninstall the native plugins/packages. Remove TokenEconomizer entries from the canonical
configuration too, so sync does not re-enable them.
See the [upstream installation guide](https://github.com/microsoft/TokenEconomizer/blob/main/plugin/README.md) for removal commands.

## Hallmark web design

[Hallmark](https://github.com/Nutlope/hallmark) is a pinned shared skill, available through the
managed skill locations for Claude Code, Codex, Cursor, OpenCode, and Copilot.
Use it for new pages. Explicitly name `hallmark redesign <path>` for existing designs,
or `hallmark audit <path>` for a report without edits.

The full setup installs its browser-check dependencies after mise tools.
For an existing environment, run `mise run hallmark-setup`. It uses mise's Node 24 and
installs pinned `puppeteer-core` into `dotfiles/.agent/node_modules`, with package scripts disabled.
It does not install Chrome. Rendering requires Chrome or Chromium; set `CHROME_PATH`
when the executable is outside the usual locations.

Upstream code, the MIT LICENSE, and photo attribution are retained. The five declared instruction
overlays in `upstreams.json` require explicit authorization for paid Together calls, existing-browser
connections, and additional hooks, and remove broad tool-permission advice.
An API key alone never authorizes image generation. Claude Code enables the upstream edit-time hook for Hallmark-marked HTML/CSS and components in a `.hallmark` project. It reports findings without blocking; no Hallmark hook is registered for Codex.
Restart the agent or start a new conversation, then invoke `/hallmark` (`$hallmark` in Codex).

## Yomiyasu Japanese revision

[Yomiyasu](https://github.com/nanaism/yomiyasu) is a pinned, unmodified shared skill.
Explicitly ask `yomiyasu` to revise a draft; existing writing skills and their selection rules remain unchanged.
Its Python standard-library tools check wording and source/final differences, then verify that the submitted text matches the checked version. These checks do not guarantee semantic correctness.
By default, checks retain source and final text in a local `.yomiyasu/` directory. Request no records when retention is unwanted; final hash verification is then unavailable.
The upstream hook is enabled in the managed Claude Code and Codex settings. It follows explicitly registered manuscripts, checks final-text records before completion or posting, and can block unchecked output. Unregistered documents are untouched. The existing Japanese prose lint hook remains enabled for general Edit/Write checks.
Restart the agent after syncing. New Codex hook definitions still require approval in `/hooks`; setup does not grant hook trust. Start a new conversation to discover the added skill.

## Canonical files and boundaries

- `dotfiles/.agent/AGENTS.md` is the shared agent policy.
- `dotfiles/.agent/apps/` contains app-specific configuration and hooks.
- `dotfiles/.agent/skills/` contains local and reviewed vendored skills.
- `dotfiles/.agent/evals/` contains Waza evaluation suites.
- `dotfiles/.agent/sync.sh` deploys the supported files to agent homes.

The repository root intentionally has no `AGENTS.md` symlink. Some managed
destinations are symlinks back to the canonical tree; a deployed file is not a
second source of truth.

The exact support matrix, file mappings, ignore rules, and hook behavior change
more often than the repository-level documentation. Read the
[AI agent directory README](../dotfiles/.agent/README.md) before editing them.

## Synchronize and verify

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
zsh tests/test_agent_support_matrix.sh
```

Use `zsh main.sh --only agent` to apply agent settings alone.
Add `--dry-run` to preview deployment without displaying secret values.
`zsh dotfiles/.agent/sync.sh --dry-run` uses the same sync implementation.

Standalone sync no longer installs dependencies automatically.
Use `zsh dotfiles/.agent/sync.sh --install-deps` when the existing Hermes MCP dependency installation is also needed.
This option changes the environment and may access the network.
Full `main.sh` setup passes it explicitly to retain its previous behavior.
See the [getting started guide](getting-started.md) for the settings and secret-file scope.

Syncing files does not prove that an already-running agent process reloaded its
configuration. Restart or reload the relevant client when its documented
behavior requires it, then verify the live configuration separately.

## Client-synced skill ownership

`dotfiles/.agent/skills/synced/` contains account-specific data generated and updated by the client.
Keep it locally, but do not distribute it through Git. CI validates tracked files and new,
non-ignored skills, and rejects accidentally tracked sync data. Duplicate canonical skill names
and missing related skills remain errors.

Fresh clones obtain synced skills and their supporting scripts through the client again.
This is separate from local skills and pinned imports in `upstreams.json`.
When an existing clone takes the untracking commit, previously tracked sync files may be removed.
Back up local customizations first, then resync through the client if needed.
This change does not alter the client's runtime skill discovery or precedence.

## Evaluate skills and prompts

Use a dry-run first when checking the Waza command routing:

```sh
mise run waza-eval-model -- --agent all --dry-run
```

The focused commands and suite layout are documented in
`dotfiles/.agent/README.md`. Evaluation output is evidence about the selected
suite and agent; it is not a substitute for sync tests or live-client checks.

## Maintain external skills with provenance

External skills are registered in `dotfiles/.agent/skills/upstreams.json` and
maintained with `scripts/agent_skill_upstreams.py`. Preserve the pinned upstream
commit, license and attribution, local overlay, security review, and focused
validation. Do not update a vendored skill by copying files directly over the
reviewed tree.

```sh
python3 scripts/agent_skill_upstreams.py check
```

## Run Claude Code with separate accounts

Requires Claude Code 2.1.261 or later. Each account uses a fixed `CLAUDE_CONFIG_DIR` with its own regular `auth login`. The [official authentication documentation](https://code.claude.com/docs/en/authentication) states that macOS Keychain entries are also scoped to this directory. A credential-free probe confirmed distinct Keychain lookups on 2.1.261.

Register each account once, checking the account and organization in the browser. Profile names use lowercase letters, numbers, dots, hyphens, and underscores. Existing shared logins and setup-tokens are not imported, so old profiles need one new login without copying tokens.

```sh
claude-account auth-login personal
claude-account auth-login work
claude-account list
```

Select an account by name. Other accounts' sessions can keep running:

```sh
claude-account personal --model fable
claude-account work --model fable --dangerously-skip-permissions
```

After exiting a rate-limited session, resume from its project directory using another account with available allowance. Other sessions are unaffected.

```sh
claude-account work --resume SESSION_ID --model fable
```

Add `--fork-session` if you want to keep the original session open. Do not update one session ID from two processes simultaneously. Resuming under another organization sends the previous conversation and code using that organization's credentials. Choose an account authorized to receive the data.

Only repeat `claude-account auth-login work` when authentication needs renewal. Login is blocked while that profile has running sessions; other profiles are unaffected. Switching profiles does not require logging in each time.

Successful login also records interactive onboarding completion. If an older registered profile shows the first-run login screen despite being authenticated, exit that profile's sessions and run `claude-account repair work`. This checks and prepares the same shared settings links as a normal launch, then verifies the current identity. It only adds the onboarding-completion flag to the profile state JSON; it does not log in again or copy credentials or project trust settings.

If the first registration used the wrong account, run `claude-account auth-login personal --replace`. Type `personal` at the confirmation prompt, then choose the correct account and organization in the browser. Ordinary login rejects a different identity. Replacement is also blocked while that profile has running sessions. Cancellation or login failure preserves the registry, but Claude itself may already have changed its saved credentials; log in again if needed.

Select the account used by ordinary `claude` launches:

```sh
claude-account default personal
claude --model fable
claude --resume SESSION_ID --model fable --dangerously-skip-permissions
claude-account default
```

The selection is stored in `~/.config/claude-account/default-profile`, or under `XDG_CONFIG_HOME` when set. It applies to Bash and Zsh after loading the updated dotfiles shell configuration, including new terminals. Running sessions keep their account. `claude-account work ...` explicitly uses `work` regardless of the default. Invalid default credentials stop the launch without falling back to another login. Replacing the selected profile changes the identity used by its future launches too.

Use `claude-account default --clear` to remove the selection. When no default is set, native Claude authentication and arguments are preserved. `command claude ...`, direct executable invocations, and scripts that do not load the shell function bypass this selection. After setting a default, use `claude-account auth-login PROFILE` for authentication, not `claude auth login`.

Profiles live in `~/.config/claude-account/accounts/PROFILE/`, or under `XDG_CONFIG_HOME` when set. Credentials, account metadata, plugins, and daemon state are independent. Existing settings.json, .mcp.json, CLAUDE.md, skills, hooks, commands, agents, rules, and projects under `~/.claude` are shared through links. Sharing projects makes existing transcripts resumable. Profile directories use mode 700, and the email-plus-organization identity hash uses mode 600. Legacy shared credentials, registries, and setup-tokens are preserved.

Arguments are forwarded except authentication-changing `--settings`, `--setting-sources`, `--managed-settings`, and `--bare`. Agent View is disabled; background, cloud, and Remote Control launches are outside this workflow. In-session `/login` and `/logout` are hidden; use `auth-login` for credential changes.

Subscription authentication does not prove Fable entitlement or remaining included allowance. The wrapper does not change models, purchase credits, or enable additional billing, but it cannot disable credits already enabled for the account. To avoid additional charges, disable usage credits in Claude's usage settings and check Fable's own allowance. Decline any credit prompt and check the plan, quota, and authentication. Real-account Fable billing and resume remain deployment acceptance checks.

CodexBar monitoring accounts are independent of this wrapper. For simultaneous display, register each account's Web session in CodexBar and check:

```sh
codexbar usage --provider claude --all-accounts --format json --pretty
```

This list does not discover `CLAUDE_CONFIG_DIR` profiles. No synchronization of setup-tokens or short-lived OAuth tokens is added. Renew expired Web sessions in CodexBar. Cookies are login credentials: never put them in Git or chat. See [CodexBar's authentication and multi-account documentation](https://github.com/steipete/CodexBar/blob/v0.56.3/docs/claude.md). You can also run `claude-account work /usage` to inspect the selected profile directly.
