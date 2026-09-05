# Configuration ownership

[日本語](configuration-ownership_JA.md) · [Documentation index](README.md)

This page is the human reference for where shell and environment changes belong.
Edit the canonical source in the repository, then use the matching apply path.
Editing only a deployed file under `$HOME` creates drift and the next apply may
overwrite it.

For shell integration, `home/.chezmoidata.toml` is the only source of values,
`home/.chezmoitemplates/dotfiles-shell-common.sh` is the only Bash/Zsh common
implementation, and `config/nix/home-manager/session.nix` reads the same TOML
instead of repeating its values.

## Ownership map

| Concern | Managed definition | Deployment or consumer |
|---|---|---|
| Shell integration values and policy | `home/.chezmoidata.toml` | chezmoi templates and the Nix projection |
| Bash/Zsh common environment and aliases | `home/.chezmoitemplates/dotfiles-shell-common.sh` | chezmoi |
| Shell startup wrappers and native adapters | `config/shell/bash*.tmpl`, `home/.chezmoitemplates/bash*`, and `home/private_dot_config/` shell templates | chezmoi |
| Nix session variables and declarative shell UI | `config/nix/home-manager/` | Home Manager or nix-darwin |
| Files rendered directly under `$HOME` | `home/` | chezmoi |
| Bash-only shell bootstrap | `scripts/setup_shell.sh` | explicit `bash` invocation |
| AI agent prompts, settings, hooks, and skills | `dotfiles/.agent/` | `dotfiles/.agent/sync.sh` |
| Repository runtime assets outside chezmoi | `dotfiles/` | component-specific scripts |
| Tool versions and task aliases | `config/mise/config.toml` | mise |
| macOS packages not available through Nix | generated `config/nix/homebrew-fallback.nix` | nix-darwin or Homebrew |
| Mac App Store applications | `config/nix/mas-apps.nix` | `scripts/install_mas_apps.sh` |

## Shell roles and startup boundaries

The interactive shell roles are intentionally narrow:

- Local interactive work uses zsh.
- Interactive sessions on servers use Bash.
- New portable scripts use Bash and a matching Bash shebang. Existing scripts
  keep their declared shebang and shell-specific behavior. This is not a plan
  to migrate every script to Bash or to promise full cross-shell parity.

### Canonical shell data

Edit `home/.chezmoidata.toml` for the shell integration values:
`EDITOR`, the four XDG defaults, named PATH candidates, safe aliases, and the
shell-specific mise policy. Each PATH candidate is a named scalar such as
`home_local_bin`, `darwin_arm64_homebrew_bin`, or
`darwin_x86_64_homebrew_bin`; its position is not encoded by an array index.
The Darwin candidates cover Apple Silicon Homebrew under `/opt/homebrew` and
Intel Homebrew under `/usr/local`.

The three native shell templates include the shared
`home/.chezmoitemplates/shell-data-validate` schema check. Invalid or unsafe
values fail during rendering; do not add a legacy key, alias, or silent
fallback for an old schema.

`config/nix/home-manager/session.nix` reads the same TOML with
`builtins.fromTOML`. Change the TOML when a default value changes; do not copy
the value into the Nix module.

### Disable optional macOS features

Set the following value in `home/.chezmoidata.toml` to `false`. Its initial
value is `true`.

```toml
[features]
macos = false
```

This disables macOS Homebrew PATH additions, the `intel`/`arm` aliases, and
Homebrew completion directories in Zsh. It also disables optional OS settings
such as Dock and Touch ID, Nix GUI packages and app copying, Homebrew/Rosetta/
Mac App Store installation, and managed Homebrew updates.
On macOS, `false` overrides `full` and `--with-gui-apps`, without changing the
profile name or Darwin backend. Nix infrastructure, CLI packages (including
platform-specific CLI tools), shared environment variables, mise, and build SDK
handling remain enabled. Linux GUI selection is unaffected.

Re-render the shell files with chezmoi after changing the flag, and reapply
the Nix configuration for Zsh completions. Nix reads the same flag through
`config/nix/features.nix`. Missing values, the string `"false"`, and unknown
keys are errors. Existing aliases in a running shell and inherited PATH
entries are not removed.

Turning this off is not a rollback. It does not uninstall existing applications
or infer previous Dock, DNS, or other OS settings to restore. Nix-managed files
and launchd definitions are updated or removed by normal generation activation.
Unmanaged cron jobs and manual `brew` commands are outside its scope.
Pre-install scripts use a dependency-free reader for the canonical bare
`[features]` table and `macos = true` / `macos = false` entry. Unlike Nix's full
TOML parser, this reader only validates that section; quoted/inline feature
forms are unsupported. There is no environment-variable override.

### Shell-specific behavior

`home/.chezmoitemplates/dotfiles-shell-common.sh` owns the shared non-secret
environment, PATH, safe aliases, Bash/Zsh-only `DOTFILES_REPO_ROOT`, and one
interactive mise activation for Bash or Zsh. The Zsh prompt, completion,
options, oh-my-zsh configuration, and the `_ssh` completion function are owned by
`config/nix/home-manager/zsh.nix`; that module does not duplicate the common
environment or mise activation.
Re-sourcing the common file does not overwrite Zsh SSH completion.

Fish is an optional native adapter at
`~/.config/fish/conf.d/zz-dotfiles.fish`. It provides the shared non-secret
environment, PATH, and interactive safe aliases, then uses the official
`mise activate fish` hook only in an interactive session. Generator and source
failures stay visible as failures.

csh/tcsh use the standalone adapter at
`~/.config/shell/dotfiles-shell-common.csh`. It provides only the limited
non-secret environment, PATH, and prompt safe aliases. Startup adds the existing
mise shim directory to PATH, so tools with installed shims can be invoked by
their normal command names. Shim-launched tools also receive `mise.toml`
environment variables. Official mise activation does not support csh/tcsh:
directory changes do not update the shell's own environment or run prompt hooks.
Use `mise exec` or `mise run` to pass the environment to commands without shims.
The adapter selects one root using non-empty `MISE_DATA_DIR`, then non-empty
`XDG_DATA_HOME`, then `HOME`. If its shim directory is missing, it does not
fall back to a lower-priority root. Neither optional adapter
provides `DOTFILES_REPO_ROOT` or reads `secrets.env`.

The Bash/Zsh common file, Fish adapter, and csh/tcsh adapter set
`MISE_GLOBAL_CONFIG_FILE` to the readable managed file
`$HOME/.config/mise/config.toml` only when the variable is unset or empty. A
non-empty explicit value is preserved. This lets a custom `XDG_CONFIG_HOME`
coexist with the managed mise tools and task configuration.

### Fixed managed paths and custom XDG

The managed shell files stay under `$HOME/.config`:

- `~/.config/shell/dotfiles-shell-common.sh`
- `~/.config/shell/dotfiles-shell-common.csh`
- `~/.config/fish/conf.d/zz-dotfiles.fish`
- `~/.config/mise/config.toml`

`XDG_CONFIG_HOME` remains an application configuration default or override; it
does not relocate these managed files. Bash and Zsh startup wrappers always
look up the common file at the fixed managed path. When a custom
`XDG_CONFIG_HOME` is active, the explicit shell bootstrap also creates the
derived Fish loader at
`$XDG_CONFIG_HOME/fish/conf.d/zz-dotfiles-canonical.fish`. The loader sources
the canonical Fish adapter under `$HOME/.config`; it is not a second chezmoi
source file. Re-run the bootstrap after changing the custom XDG path.

Run `scripts/setup_shell.sh`, described below, to register automatic csh/tcsh
startup. A chezmoi apply alone does not register it. The bootstrap adds a
managed source block to `.cshrc` and to `.tcshrc` only if the latter already
exists. It creates a missing `.cshrc`, but never creates a new `.tcshrc`, which
would stop tcsh from reading an existing `.cshrc`.

Existing file content and modes are preserved. A separating newline is added
when the original file has no final newline. If an older manual `source` line
loads this adapter, remove that line before running the bootstrap; the bootstrap preserves
user text and does not detect, migrate, or remove that line. User settings added
after the managed block are also preserved. The managed block's `df_csh_loaded`
guard loads the common adapter once per shell process, including when `.tcshrc`
sources `.cshrc`; this exact-once guarantee applies only through that managed
block. `df_csh_loaded` is reserved for the managed block and should not be set
by user startup code. Start a new
shell after re-rendering the configuration.

## Bash-only shell bootstrap

Use `scripts/setup_shell.sh` when a host needs the shell integration without the
full Nix profile. Invoke it with Bash 3.2 or later:

```sh
bash scripts/setup_shell.sh --dry-run
bash scripts/setup_shell.sh
bash scripts/setup_shell.sh --verify
```

The bootstrap requires Bash, chezmoi, and standard Unix utilities. It does not
install packages, Nix, Homebrew, mise, or another shell; it does not change the
login shell or run `chsh`. It also does not modify `.profile`,
`~/.config/fish/config.fish`, `secrets.env`, or unrelated application
files.

The chezmoi apply allowlist is exactly these six targets:

| Target | Role |
|---|---|
| `~/.bashrc` | Bash startup wrapper |
| `~/.bash_profile` | Bash login wrapper |
| `~/.config/shell/dotfiles-shell-common.sh` | Bash/Zsh common implementation |
| `~/.config/shell/dotfiles-shell-common.csh` | csh/tcsh standalone adapter |
| `~/.config/fish/conf.d/zz-dotfiles.fish` | Fish adapter |
| `~/.config/mise/config.toml` | rendered repository mise configuration |

With a custom `XDG_CONFIG_HOME`, the Fish loader described above is an
additional derived file. It is generated only for that custom path and is not
counted as another managed source target.
The csh/tcsh startup blocks described above are also registered separately
from these six rendered targets.

Before writing, the bootstrap compares existing targets and refuses a foreign
or different file. The same rule applies to the custom-XDG Fish loader. An
identical existing file is safe to reuse; `--force` is required to replace a
different file. `--dry-run` writes nothing. `--verify` checks the rendered
targets, csh/tcsh startup blocks, and Fish loader. A malformed or duplicate
csh/tcsh marker, symlink, hard link, or directory is rejected even with `--force`.
Hard-linked startup files are not appended to because other file names would
also be modified.
`--help` prints the current interface.
Read the command's `--help` output if this page and the installed script ever
disagree.

## Cross-platform validation boundaries

The shell checks distinguish an unavailable optional runtime from a failed
available runtime. Fish is `PASS` when its required check succeeds, `FAIL` when
the binary is available but the check or activation fails, and not-applicable
`SKIP` only when the binary is unavailable. csh/tcsh checks record runtime
identity; an implementation that is merely the same binary under two names is
not evidence of a separately verified genuine csh runtime.

The CI design exercises real Fish/csh/tcsh runtimes and shell-only cells for
Intel macOS, Debian, and Fedora. A CI result applies to the exact runner and
commit that produced it; this documentation does not claim that every
distribution, kernel, or shell implementation has passed. WSL kernel behavior,
BSD hosts, and an actual RHEL host remain unverified boundaries. Run the
focused checks on a target host before relying on an environment outside the
tested cells.

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

The small wrappers under `config/shell/` and
`home/.chezmoitemplates/bash*` must stay aligned. The Bash/Zsh common
implementation is maintained only in
`home/.chezmoitemplates/dotfiles-shell-common.sh`; the removed large mirror is
not a second edit location.

For the full chezmoi path, preview and verify before applying:

```sh
zsh scripts/chezmoi_apply.sh --dry-run
zsh scripts/chezmoi_apply.sh --mark-default
zsh scripts/chezmoi_apply.sh --verify
```

`--mark-default` records `chezmoi` in `~/.config/dotfiles/manager` and stores
the selected profile in `~/.config/dotfiles/profile`. The Bash-only bootstrap
does not write these markers.

## Mise owns tool versions and task aliases

`config/mise/config.toml` declares mise-managed tools and repository task
aliases. Use the smallest task that matches the intended update. Moving to a
new major release line, such as `node@22`, requires an explicit configuration
change rather than a general update command.

The shell integration policy is separate in `home/.chezmoidata.toml`: Fish is
`interactive-only`, while csh/tcsh are `unsupported-activation`. The csh/tcsh
adapter uses shim precedence `MISE_DATA_DIR`, then `XDG_DATA_HOME`, then
`HOME`; Fish delegates to its official activation hook. This policy supplies
shell integration data; it does not add csh/tcsh activation or replace the
tool versions and task aliases in `config/mise/config.toml`.

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
whole. Keep shell policy details in this ownership guide and keep the shared
prompt's role note short.

See [AI agent configuration](ai-agents.md) for the supported surfaces and
validation workflow.
