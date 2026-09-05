# Getting started

[日本語](getting-started_JA.md) · [Documentation index](README.md)

Choose the setup path that matches the host:

- Use `main.sh` for the full Nix/Home Manager environment.
- Use `bash scripts/setup_shell.sh` when the host needs only the portable shell
  integration and already has Bash and chezmoi.

The shell-only path is deliberately narrow. It does not install packages, Nix,
Homebrew, mise, or another shell, and it does not change the login shell.

## Install prerequisites

Install missing tools through the operating system's package manager or their
official documentation. The repository does not ask you to paste an unreviewed
download-and-execute pipeline.

- Git: [Installing Git in the Pro Git book](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- Bash: the [GNU Bash Reference Manual](https://www.gnu.org/software/bash/manual/)
- chezmoi: [official installation instructions](https://www.chezmoi.io/install/)
- Nix for the full path: the [official Nix download page](https://nixos.org/download/)
- mise, when tool version management is needed: [official mise installation instructions](https://mise.jdx.dev/installing-mise.html)

### Full Nix setup requirements

- macOS or Linux on `aarch64` or `x86_64`.
- Git and zsh available before running the bootstrap command.
- Network access for the initial clone and package downloads.
- Administrator access for the normal Nix installation on macOS. Linux must
  already have Nix in `PATH`; otherwise install it first or use the
  nix-portable path below.
- A signed-in Mac App Store account if the `full` profile should install apps
  listed in `config/nix/mas-apps.nix`.

### Bash-only setup requirements

- Bash 3.2 or later. The system Bash on older macOS hosts is intentionally
  within the supported syntax range.
- chezmoi available as an executable.
- Git to obtain this repository and standard Unix utilities such as `mkdir`,
  `mktemp`, `cmp`, and `chmod`.

The Bash-only command does not require Nix, zsh, mise, Fish, csh, or tcsh to
render its six allowlisted targets. Fish and csh/tcsh are optional runtimes for
the adapters after deployment.

This is a personal environment repository. Read the [configuration ownership
guide](configuration-ownership.md) before applying it to a machine with an
existing, independently managed setup.

## Bash-only shell onboarding

Clone the repository, inspect the command interface, preview the six-target
allowlist, then apply it:

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
bash --version
chezmoi --version
bash scripts/setup_shell.sh --help
bash scripts/setup_shell.sh --dry-run
bash scripts/setup_shell.sh
bash scripts/setup_shell.sh --verify
```

The bootstrap keeps managed shell files under `$HOME/.config`, even when
`XDG_CONFIG_HOME` points elsewhere. With a custom XDG config directory it also
creates a derived Fish loader under that directory so Fish can load the
canonical adapter. It does not modify `.profile`; it registers a managed
csh/tcsh startup block by appending to `.cshrc` and an existing `.tcshrc`.
It creates a missing `.cshrc` but never creates `.tcshrc`. If an older manual
`source` line loads `~/.config/shell/dotfiles-shell-common.csh`, remove that line before running the bootstrap; the
bootstrap preserves that user text and does not detect or migrate the line.
See [Shell roles and startup
boundaries](configuration-ownership.md#shell-roles-and-startup-boundaries).

Before writing, the bootstrap refuses an existing target whose content differs
from the rendered file. An identical target can be reused; `--force` is the
explicit option for replacing a different target. Review the command's
`--help` output as the authoritative list of flags.

## Choose a full profile

For a new machine that should receive the Nix/Home Manager environment, use
`main.sh` after satisfying the Nix prerequisite. It selects a profile, applies
Nix and chezmoi, installs optional Mac App Store apps, and configures repository
Git hooks. On macOS it can install Nix when needed; on Linux it requires an
existing Nix installation.

`main.sh` selects a default based on the operating system:

- `full` is the default on macOS. It includes nix-darwin, Home Manager, GUI
  apps, macOS defaults, user timers, mise tools, and home files.
- `cli` is the default on Linux. It omits GUI apps, macOS-only settings, and
  user timers, then applies the shared CLI package set and home files.

On macOS, the `full` examples below assume that `features.macos` is `true` (the
default). Setting it to `false` disables optional OS settings and macOS app
installation while retaining the Nix/CLI setup. See [Disable
optional macOS features](configuration-ownership.md#disable-optional-macos-features).

Run the CLI profile explicitly when you want a smaller macOS setup:

```sh
zsh main.sh --cli-only
```

Run the full macOS setup without Mac App Store apps when the account is not
ready or one of the listed apps is unavailable:

```sh
zsh main.sh --full --skip-mas-apps
```

On Linux, confirm `nix --version` works before running `main.sh`. If Nix is not
available, install it using the host's approved method or use nix-portable.

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

The full setup is intended to be rerunnable. Read an error before retrying: a
fresh shell may be required after the first Nix installation. When
`features.macos` is `true` and fallback entries exist, Homebrew may still be
required.

### Linux without sudo

Use nix-portable when the host cannot create or mount the normal `/nix` store:

```sh
zsh scripts/nix_portable_install.sh
export PATH="$HOME/.local/bin:$PATH"
nixp --version
dotfiles-nix-shell

# Run one command inside the repository CLI package set.
dotfiles-nix-run git --version
```

The default nix-portable runtime is `proot`, which also works when mount
namespaces are restricted. `scripts/nix_rootless_install.sh` remains available
for `nix-user-chroot`, but its store is visible only inside the chroot. This is
an alternative Nix path, not a compatibility claim for every WSL kernel, BSD
host, or RHEL installation.

## Verify the result

For the Bash-only path, `--verify` checks the six shell targets, managed csh/tcsh
startup blocks, and any derived custom-XDG Fish loader without changing them.

For the full path, run the non-mutating checks first:

```sh
# Verify deployed chezmoi files without changing them.
zsh scripts/chezmoi_apply.sh --verify

# Build the CLI Nix configuration without switching to it.
zsh scripts/nix_install.sh --cli-only --dry-run

# Run repository checks.
zsh tests/run.sh
```

`chezmoi_apply.sh --verify` exits with status 0 when every managed home target
matches, and 1 when drift exists. The Nix dry-run builds the selected flake
output but does not switch the active system or Home Manager generation.

For the shell runtime result rules and platform boundary, see [Cross-platform
validation boundaries](configuration-ownership.md#cross-platform-validation-boundaries).
The CI configuration is designed to exercise real Fish/csh/tcsh runtimes plus
Intel macOS, Debian, and Fedora shell-only cells. Check the exact CI run before
treating a platform as verified; WSL kernel behavior, BSD, and actual RHEL
remain outside the verified boundary.

## Continue with the ownership guide

Before editing a deployed file, read [Configuration ownership](configuration-ownership.md).
It explains whether the canonical source belongs in Nix, `config/`, `home/`, the
Bash-only bootstrap, or the shared agent tree.
