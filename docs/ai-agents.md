# AI agent configuration

[日本語](ai-agents_JA.md) · [Documentation index](README.md)

Shared AI agent files are maintained under `dotfiles/.agent/`. Keep changes in
that canonical tree, run the sync, and validate both the managed source and
representative deployed targets.

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

Syncing files does not prove that an already-running agent process reloaded its
configuration. Restart or reload the relevant client when its documented
behavior requires it, then verify the live configuration separately.

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

Profiles live in `~/.config/claude-account/accounts/PROFILE/`, or under `XDG_CONFIG_HOME` when set. Credentials, account metadata, plugins, and daemon state are independent. Existing settings.json, .mcp.json, CLAUDE.md, skills, hooks, commands, agents, rules, and projects under `~/.claude` are shared through links. Sharing projects makes existing transcripts resumable. Profile directories use mode 700, and the email-plus-organization identity hash uses mode 600. Legacy shared credentials, registries, and setup-tokens are preserved.

Arguments are forwarded except authentication-changing `--settings`, `--setting-sources`, `--managed-settings`, and `--bare`. Agent View is disabled; background, cloud, and Remote Control launches are outside this workflow. In-session `/login` and `/logout` are hidden; use `auth-login` for credential changes.

Subscription authentication does not prove Fable entitlement or remaining included allowance. The wrapper does not change models, purchase credits, or enable additional billing, but it cannot disable credits already enabled for the account. To avoid additional charges, disable usage credits in Claude's usage settings and check Fable's own allowance. Decline any credit prompt and check the plan, quota, and authentication. Real-account Fable billing and resume remain deployment acceptance checks.

CodexBar monitoring accounts are independent of this wrapper. For simultaneous display, register each account's Web session in CodexBar and check:

```sh
codexbar usage --provider claude --all-accounts --format json --pretty
```

This list does not discover `CLAUDE_CONFIG_DIR` profiles. No synchronization of setup-tokens or short-lived OAuth tokens is added. Renew expired Web sessions in CodexBar. Cookies are login credentials: never put them in Git or chat. See [CodexBar's authentication and multi-account documentation](https://github.com/steipete/CodexBar/blob/v0.56.3/docs/claude.md). You can also run `claude-account work /usage` to inspect the selected profile directly.
