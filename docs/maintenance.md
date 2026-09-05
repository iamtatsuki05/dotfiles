# Maintenance

[日本語](maintenance_JA.md) · [Documentation index](README.md)

Update the repository first, then apply only the configuration layers that
changed. Pull hooks keep lightweight managed files synchronized, but they do
not replace an explicit Nix or mise update.

## Update an existing clone

```sh
cd ~/src/dotfiles
git pull --ff-only

# Preview and apply chezmoi-managed home files.
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default

# Build-check and apply the CLI Nix profile.
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

For a desktop host that should also update GUI applications:

```sh
zsh scripts/nix_install.sh --with-gui-apps
```

Run the Nix step whenever `flake.nix`, `flake.lock`, or `config/nix/` changes.
Run the agent sync after changes under `dotfiles/.agent/`.

## Choose the smallest update task

```sh
# Update only flake.lock.
mise run lock-update

# Update only the nixpkgs input.
mise run lock-update-nixpkgs

# Update and apply Nix-managed tools.
mise run nix-update

# Refresh nixpkgs, then apply the Nix package set.
mise run nixpkgs-update

# Update mise-managed tools within configured release lines.
mise run mise-update

# Update Nix, mise-managed tools, and Hermes Agent.
mise run package-update
```

When `features.macos` is `true` (the default), `mise run package-update` uses the
CLI Nix profile by default on macOS when Homebrew GUI fallback entries exist.
Add `-- --with-gui-apps` to apply both the Nix GUI package set and Homebrew-managed
GUI fallback apps. Use
`mise run hermes-update` when Hermes Agent is the only target.

With `features.macos` set to `false` on macOS, `--with-gui-apps` does not enable
GUI packages or managed Homebrew updates. CLI packages and mise remain enabled.
See [Disable optional macOS features](configuration-ownership.md#disable-optional-macos-features).

## Understand pull hooks

`main.sh` installs three repository-local Git hooks:

- `post-merge` runs after a merge or ordinary `git pull`.
- `post-rewrite` runs after operations such as `git pull --rebase`.
- `post-checkout` runs after a branch checkout.

They call `scripts/apply_updates.sh`, which applies chezmoi files, synchronizes
AI agent files, and refreshes the hooks. They do not switch nix-darwin or Home
Manager, uninstall Homebrew, or install mise tools.

Reinstall them manually with:

```sh
zsh scripts/setup_git_hooks.sh
```

## Scheduled repository pulls

The `full` profile declares `dotfiles-auto-update` as a nix-darwin launchd
agent on macOS and a Home Manager systemd user timer on Linux. It runs
`git pull --ff-only` in `${HOME}/src/dotfiles` every day at 06:00 and writes
logs to `/tmp/dotfiles-git-pull.log`.

During macOS activation, the nix-darwin module also removes the legacy managed
cron block when it is still present.

This scheduled pull has the same boundary as the Git hooks: it does not make a
new Nix generation merely because the flake changed.

## Run checks before sharing a change

```sh
# Full local suite.
zsh tests/run.sh

# Equivalent mise task.
mise run dotfiles-test

# Focused checks for common ownership boundaries.
zsh tests/test_chezmoi_source_state.sh
zsh tests/test_chezmoi_rendered_home.sh
zsh tests/test_nix_migration.sh
zsh tests/test_agent_sync.sh
```

The test runner checks shell syntax, helper scripts, generated chezmoi source
drift, and rendering into a temporary home. The rendered-home integration
check is skipped when chezmoi is unavailable locally. GitHub Actions installs
chezmoi and runs the suite on macOS and Ubuntu.

## Reclaim package-manager space carefully

Cleanup is a dry-run unless `--apply` is supplied:

```sh
mise run package-cleanup
mise run package-cleanup -- --apply
mise run package-cleanup -- --include-mise
mise run package-cleanup -- --include-mise --apply
```

The task can delete old Nix generations and caches. Removing generations
reduces rollback history. `--include-mise` also prunes unused mise tool versions
and stale mise caches. Review the printed targets before applying cleanup.
