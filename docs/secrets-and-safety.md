# Secrets and safety

[日本語](secrets-and-safety_JA.md) · [Documentation index](README.md)

Store real credentials only in local, ignored files or the operating system's
credential store. Repository templates describe expected variable names but
must never contain usable secret values.

## Local shell secrets

The managed local file is `~/.config/shell/secrets.env`. On first setup,
chezmoi creates it from `config/shell/secrets.env.example` when it does not
already exist. Fill in only the values needed on that machine, then restart the
shell.

Typical variable names include:

```sh
export SLACK_WEBHOOK_URL=""
export OPENAI_API_KEY=""
export ANTHROPIC_API_KEY=""
export GEMINI_API_KEY=""
export GITHUB_TOKEN=""
export OPENCODE_API_KEY=""
export DEVIN_API_KEY=""
```

Do not put a real webhook URL, token, password, private key, or exported
credential in `config/`, `home/`, documentation, test fixtures, or session
logs. Before sharing a diff, inspect both tracked and untracked files.

## Agent sync derives local environment files

`dotfiles/.agent/sync.sh` reads the local secrets file and updates these local
agent environment files with mode 600:

- `~/.gemini/antigravity-cli/.env`: `DEVIN_API_KEY`
- `~/.hermes/.env`: `DEVIN_API_KEY`, `OPENCODE_API_KEY`, and
  `OPENCODE_GO_API_KEY` derived from the OpenCode value
- `~/.openclaw/.env`: `DEVIN_API_KEY` and `OPENCODE_API_KEY`

The sync writes local ignored files; it does not send these values to an
external service. The files are still credential copies, so exclude them from
backups, diagnostics, and support bundles that may be shared.

## Shell startup ownership

Chezmoi renders bash startup files and
`~/.config/shell/dotfiles-shell-common.sh`. Home Manager's zsh configuration
sources the same common file when it exists. Put shared environment loading in
the canonical templates; do not copy secret values into each shell config.

The Bash/Zsh common file is the managed path that may source
`~/.config/shell/secrets.env`; it also carries the Bash/Zsh-only
`DOTFILES_REPO_ROOT`. The optional Fish adapter provides the non-secret
environment and PATH, interactive safe aliases, and, in an interactive Fish
session, the official `mise activate fish` hook. The standalone csh/tcsh
adapter provides its limited non-secret environment, PATH, and prompt safe
aliases, but no mise activation. Neither adapter may source `secrets.env`,
expand its values, or copy them into shell state or provide
`DOTFILES_REPO_ROOT`. Use the tool's approved credential store or an explicit
command when authentication is needed from Fish or csh/tcsh.

## Preview changes before applying them

Use non-mutating modes whenever they are available:

```sh
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --verify
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/migrate_brew_to_nix.sh
zsh scripts/remove_homebrew.sh --dry-run
mise run package-cleanup
```

Read the resolved targets and selected profile before running the corresponding
apply operation.

## Know which operations reduce recovery options

- Removing Homebrew deletes a package manager and may remove packages that are
  not yet represented in Nix. The script refuses removal while declared
  fallback entries remain, but local untracked use still needs review.
- Package cleanup can delete old Nix generations, reducing rollback history.
- Applying chezmoi can overwrite drift in managed home files. Preview the diff
  and move any intended local change back to the canonical source first.
- A Nix switch changes the active system or Home Manager generation. Build with
  `--dry-run` before switching after meaningful configuration changes.

The first nix-darwin apply preserves an existing `/etc/pam.d/sudo_local` at
`/etc/pam.d/sudo_local.before-nix-darwin`. Keep that backup until sudo Touch ID
behavior has been verified.

## Treat authentication changes as exclusive operations

Claude Code profile switching changes one shared macOS Keychain credential.
Exit all Claude processes before `claude-account auth-login`. Do not bypass the
profile wrapper with a plain Claude launch during multi-account use. See
[AI agent configuration](ai-agents.md) for the full procedure.
