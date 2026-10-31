# dotfiles

[日本語](README_JA.md)

Personal dotfiles for reproducible macOS and Linux environments. Nix manages
packages and declarative shell configuration, while chezmoi deploys files that
belong directly under `$HOME`.

## Quick start

On macOS, `main.sh` installs Nix when needed. On Linux, install Nix first or
use the [nix-portable path](docs/getting-started.md#linux-without-sudo).

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

`main.sh` selects `full` on macOS and `cli` on Linux. Review the
[getting-started guide](docs/getting-started.md) before using a different
profile or setting up a restricted Linux host.

## Documentation

- [Getting started](docs/getting-started.md): prerequisites, profiles, initial
  setup, and post-install checks.
- [Configuration ownership](docs/configuration-ownership.md): which files are
  managed by Nix, chezmoi, mise, Homebrew, and the repository.
- [Maintenance](docs/maintenance.md): updating an existing clone, tests,
  automatic synchronization, and cleanup.
- [Package management and migration](docs/package-management.md): Nix package
  lists, Homebrew and Mac App Store fallbacks, and migration commands.
- [AI agent configuration](docs/ai-agents.md): shared prompts, app settings,
  synchronization, evaluation, and Claude Code profiles.
- [Secrets and safety](docs/secrets-and-safety.md): local credentials,
  destructive commands, backups, and dry-run conventions.
- [Troubleshooting](docs/troubleshooting.md): common setup, drift, Nix,
  Homebrew, and agent-sync failures.

The [documentation index](docs/README.md) also links to the focused READMEs
inside `config/`, `home/`, `scripts/`, `tests/`, and `dotfiles/.agent/`.

## Common commands

```sh
# Preview home-file changes.
zsh scripts/chezmoi_apply.sh --dry-run

# Build the CLI Nix configuration without switching.
zsh scripts/nix_install.sh --cli-only --dry-run

# Run the repository checks.
zsh tests/run.sh
```

For changes to `flake.nix`, `flake.lock`, or `config/nix/`, apply Nix
explicitly. Git pull hooks do not run a Nix switch or install mise tools.
