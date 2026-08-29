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

## Use Claude Code login profiles safely

`claude-account` uses the single full-scope `claude auth login` credential in
the macOS Keychain. macOS cannot keep an independent full-login credential for
each profile, so switching profiles requires browser authentication.

Before registering or switching, exit every Claude Code session. Sessions
started through `claude-account` hold a shared lock, and plain `claude`
processes are detected separately.

```sh
pgrep -fl claude
# Continue only when no Claude process remains.

claude-account auth-login <profile>
# Select the intended account in the browser.
```

The command stores a SHA-256 fingerprint derived from the email and
organization ID, plus the subscription type, in
`~/.config/claude-account/login-profiles.json` with mode 600. It does not store
the email or organization ID itself.

List the registered mappings and launch only through a matching profile:

```sh
claude-account list
claude-account <profile> --model fable
claude-account <profile> --resume <session-id> --model fable
```

The wrapper removes API keys, custom endpoints, and Bedrock, Vertex, or
Foundry selectors from the child process. It fails closed when inspectable
settings contain `apiKeyHelper` or authentication environment overrides.
Plain `claude` and `claude-auto` bypass the profile identity check and should
not be used for this multi-account workflow.
