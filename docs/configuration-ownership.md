# Configuration ownership

[日本語](configuration-ownership_JA.md) · [Documentation index](README.md)

Edit the canonical source in this repository, then use the corresponding apply
command. Editing only the deployed file under `$HOME` creates drift and the
next apply may overwrite it.

## Ownership map

| Concern | Managed definition | Deployment path |
|---|---|---|
| Packages and declarative shell behavior | `flake.nix`, `config/nix/` | nix-darwin or Home Manager |
| Terminal, bash, mise, and local templates | `config/` and generated source in `home/` | chezmoi |
| Files directly rendered under `$HOME` | `home/` | chezmoi |
| AI agent prompts, settings, hooks, and skills | `dotfiles/.agent/` | `dotfiles/.agent/sync.sh` |
| Repo-level runtime assets outside chezmoi | `dotfiles/` | Component-specific scripts |
| Tool versions and task commands | `config/mise/config.toml` | mise |
| macOS packages not available through Nix | Generated `config/nix/homebrew-fallback.nix` | nix-darwin/Homebrew |
| Mac App Store applications | `config/nix/mas-apps.nix` | `scripts/install_mas_apps.sh` |

## Nix owns packages and declarative shell configuration

macOS uses nix-darwin with Home Manager. Linux uses standalone Home Manager
with the same package lists. Zsh and Neovim are declared under
`config/nix/home-manager/`; macOS defaults and the scheduled update agent are
declared under `config/nix/darwin/`.

The macOS defaults include keyboard repeat, sudo Touch ID, and a screenshot
directory at `${HOME}/SS`. Review `config/nix/darwin/defaults.nix` before
applying this personal preference set to another Mac.

Apply this layer after changing `flake.nix`, `flake.lock`, or `config/nix/`:

```sh
zsh scripts/nix_install.sh --cli-only --dry-run
zsh scripts/nix_install.sh --cli-only
```

Use `--with-gui-apps` when the selected host should also apply GUI packages.

## Chezmoi owns files rendered into the home directory

The repository root `.chezmoiroot` points to `home/`. Files named `dot_*` and
files under `private_dot_config/` are rendered into their corresponding paths
under `$HOME`.

Some source files live under `config/` and are mirrored into chezmoi templates.
Keep both sides aligned; `tests/test_chezmoi_source_state.sh` detects drift.

```sh
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default
zsh scripts/chezmoi_apply.sh --verify
```

`--mark-default` records `chezmoi` in `~/.config/dotfiles/manager` and stores
the selected profile in `~/.config/dotfiles/profile`.

## Mise owns tool versions and task aliases

`config/mise/config.toml` declares mise-managed tools and repository task
aliases. Use the smallest task that matches the intended update. Moving to a
new major release line, such as `node@22`, requires an explicit configuration
change rather than a general update command.

## Homebrew and MAS are bounded fallbacks

The package preference is `Nix > Homebrew > MAS`. Homebrew is retained only for
entries that are not currently migrated to Nix. Mac App Store applications are
installed separately so one unavailable app does not fail the Nix activation.

See [Package management and migration](package-management.md) before changing
fallback lists or removing Homebrew.

## Shared agent files have a separate sync step

`dotfiles/.agent/AGENTS.md` is the canonical shared prompt. App-specific
configuration, hooks, skills, and evaluations live beside it. The repository
root intentionally has no `AGENTS.md` symlink.

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
```

See [AI agent configuration](ai-agents.md) for the supported surfaces and
validation workflow.
