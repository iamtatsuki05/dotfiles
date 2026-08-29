# Getting started

[日本語](getting-started_JA.md) · [Documentation index](README.md)

Use `main.sh` for a new machine after satisfying the Nix prerequisite below.
It selects a profile, applies Nix and chezmoi, installs optional Mac App Store
apps, and configures repository Git hooks. On macOS it can install Nix when
needed; on Linux it requires an existing Nix installation.

## Requirements

- macOS or Linux on `aarch64` or `x86_64`.
- Git and zsh available before running the bootstrap command.
- Network access for the initial clone and package downloads.
- Administrator access for the normal Nix installation on macOS. Linux must
  already have Nix in `PATH`; otherwise install Nix first or use the
  nix-portable path below.
- A signed-in Mac App Store account if the `full` profile should install apps
  listed in `config/nix/mas-apps.nix`.

This is a personal environment repository. Read the configuration and scripts
before applying it to a machine with an existing, independently managed setup.

## Choose a profile

`main.sh` selects a default based on the operating system:

- `full` is the default on macOS. It includes nix-darwin, Home Manager, GUI
  apps, macOS defaults, user timers, mise tools, and home files.
- `cli` is the default on Linux. It omits GUI apps, macOS-only settings, and
  user timers, then applies the shared CLI package set and home files.

Run the CLI profile explicitly when you want a smaller macOS setup:

```sh
zsh main.sh --cli-only
```

Run the full macOS setup without Mac App Store apps when the account is not
ready or one of the listed apps is unavailable:

```sh
zsh main.sh --full --skip-mas-apps
```

## Install a new machine

On Linux, confirm `nix --version` works before running `main.sh`. If Nix is not
available, install it using the host's approved method or use nix-portable.

```sh
git clone https://github.com/iamtatsuki05/dotfiles.git
cd dotfiles
zsh main.sh
```

The setup is intended to be rerunnable. Read an error before retrying: a fresh
shell may be required after the first Nix installation, and Homebrew may still
be required when fallback entries exist.

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
for `nix-user-chroot`, but its store is visible only inside the chroot.

## Verify the result

Run the non-mutating checks first:

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

## Continue with the ownership guide

Before editing a deployed file, read [Configuration ownership](configuration-ownership.md).
It explains whether the canonical source belongs in Nix, `config/`, `home/`, or
the shared agent tree.
