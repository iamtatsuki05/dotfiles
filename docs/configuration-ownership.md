# Configuration ownership

[日本語](configuration-ownership_JA.md) · [Documentation index](README.md)

Edit the canonical source in this repository, then use the corresponding apply
command. Editing only the deployed file under `$HOME` creates drift and the
next apply may overwrite it.

## Ownership map

| Concern | Managed definition | Deployment path |
|---|---|---|
| Packages and declarative shell behavior | `flake.nix`, `config/nix/` | nix-darwin or Home Manager |
| Terminal, shell adapters, bash, mise, and local templates | `config/` and generated source in `home/` | chezmoi |
| Files directly rendered under `$HOME` | `home/` | chezmoi |
| AI agent prompts, settings, hooks, and skills | `dotfiles/.agent/` | `dotfiles/.agent/sync.sh` |
| Repo-level runtime assets outside chezmoi | `dotfiles/` | Component-specific scripts |
| Tool versions and task commands | `config/mise/config.toml` | mise |
| macOS packages not available through Nix | Generated `config/nix/homebrew-fallback.nix` | nix-darwin/Homebrew |
| Mac App Store applications | `config/nix/mas-apps.nix` | `scripts/install_mas_apps.sh` |

## Shell roles and startup boundaries

The interactive shell roles are intentionally narrow:

- Local interactive work uses zsh.
- Interactive sessions on servers use Bash.
- New portable scripts use Bash and a matching Bash shebang. Existing scripts
  keep their declared shebang and shell-specific behavior. This is not a plan
  to migrate every script to Bash or to claim full cross-shell parity.

Chezmoi renders the Bash/Zsh common environment, PATH, safe aliases, and
`DOTFILES_REPO_ROOT` from the canonical shell templates. Fish receives an
optional adapter at `~/.config/fish/conf.d/zz-dotfiles.fish` for the shared
non-secret environment and PATH, plus interactive safe aliases. In an
interactive Fish session it also sources the official `mise activate fish`
hook. csh/tcsh receive a standalone adapter at
`~/.config/shell/dotfiles-shell-common.csh` for its limited non-secret
environment, PATH, and prompt safe aliases; it does not provide activation.
Neither adapter provides `DOTFILES_REPO_ROOT`, the full zsh UI, or
`secrets.env`.

Fish's official `mise activate fish` hook runs only in an interactive Fish
session. A generator or source failure must remain visible as an explicit
failure; it is not silently ignored. csh/tcsh activation is unsupported. Use
mise shims or `mise exec`/`mise run` for those shells instead.

The csh/tcsh adapter is standalone and is not added to existing startup files.
Opt in manually from an existing `.cshrc` or `.tcshrc`:

```csh
if (-r "$HOME/.config/shell/dotfiles-shell-common.csh") source "$HOME/.config/shell/dotfiles-shell-common.csh"
```

The verification matrix runs required Fish runtime checks when a Fish binary is
available on PATH or in a cached Nix runtime. A successful check is `PASS`; if
the binary is unavailable, and only then, the cell is not-applicable `SKIP`.
An available Fish runtime whose check or activation fails is `FAIL`. A genuine
csh implementation remains a separate unverified boundary and is not reported
as `PASS`. A not-applicable `SKIP` is not a runtime `PASS`.

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

Chezmoi's shell adapter policy is declared separately in `home/.chezmoidata.toml`:
Fish uses `interactive-only` activation, while csh/tcsh use
`unsupported-activation`. The csh/tcsh standalone adapters use shim precedence
`MISE_DATA_DIR`, then `XDG_DATA_HOME`, then `HOME`; Fish delegates to its
official activation hook and does not consume this list. This policy supplies
shell integration data; it does not add csh/tcsh activation support or replace
the tool versions and task aliases in `config/mise/config.toml`.

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

The sync test validates the canonical prompt through its managed links as a
whole. Keep shell policy text in `AGENTS.md` rather than duplicating individual
shell-policy literals in that test.

See [AI agent configuration](ai-agents.md) for the supported surfaces and
validation workflow.
