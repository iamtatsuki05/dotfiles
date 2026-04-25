#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly NIX_PORTABLE_BIN_DIR="${NIX_PORTABLE_BIN_DIR:-$HOME/.local/bin}"
readonly NIX_PORTABLE_BIN="$NIX_PORTABLE_BIN_DIR/nix-portable"
readonly NIX_PORTABLE_URL_BASE="https://github.com/DavHau/nix-portable/releases/latest/download"
readonly NIX_PORTABLE_RUNTIME_DEFAULT="${NIX_PORTABLE_RUNTIME_DEFAULT:-proot}"

MODE="install"
PROFILE="cli"

usage() {
  cat <<EOF
Usage:
  zsh scripts/nix_portable_install.sh [options]
  zsh scripts/nix_portable_install.sh --shell [--profile cli|full]
  zsh scripts/nix_portable_install.sh --run COMMAND [ARGS...]
  zsh scripts/nix_portable_install.sh --nix ARGS...

Options:
  --profile cli|full       Select dotfiles package profile for --shell/--run.
  --cli-only               Alias for --profile cli.
  --with-gui-apps          Alias for --profile full.
  --shell                  Enter a shell with the selected Nix package set.
  --run COMMAND [ARGS...]  Run COMMAND with the selected Nix package set.
  --nix ARGS...            Run raw Nix via nix-portable.
  -h, --help               Show this help.

This is the sudo-free Linux path. It uses nix-portable, so packages are
available inside nix-portable commands without creating a system /nix.
EOF
}

package_attr() {
  case "$PROFILE" in
    cli)
      print -r -- "dotfiles-cli-packages"
      ;;
    full)
      print -r -- "dotfiles-full-packages"
      ;;
    *)
      echo "ERROR: unknown profile: $PROFILE" >&2
      return 1
      ;;
  esac
}

install_nix_portable() {
  local url

  if [[ -x "$NIX_PORTABLE_BIN" ]]; then
    return 0
  fi

  mkdir -p "$NIX_PORTABLE_BIN_DIR"
  url="$NIX_PORTABLE_URL_BASE/nix-portable-$(uname -m)"
  curl -fL "$url" -o "$NIX_PORTABLE_BIN"
  chmod +x "$NIX_PORTABLE_BIN"
}

nix_portable() {
  NP_RUNTIME="${NP_RUNTIME:-$NIX_PORTABLE_RUNTIME_DEFAULT}" "$NIX_PORTABLE_BIN" "$@"
}

flake_package() {
  print -r -- "path:$REPO_ROOT#$(package_attr)"
}

run_raw_nix() {
  nix_portable nix "$@"
}

run_with_package_set() {
  local package
  package="$(flake_package)"
  nix_portable nix shell "$package" -c "$@"
}

enter_package_shell() {
  local shell_path="${DOTFILES_NIX_PORTABLE_SHELL:-${SHELL:-/usr/bin/zsh}}"
  run_with_package_set "$shell_path" -l
}

write_wrappers() {
  local nixp_wrapper="$NIX_PORTABLE_BIN_DIR/nixp"
  local shell_wrapper="$NIX_PORTABLE_BIN_DIR/dotfiles-nix-shell"
  local run_wrapper="$NIX_PORTABLE_BIN_DIR/dotfiles-nix-run"

  mkdir -p "$NIX_PORTABLE_BIN_DIR"

  cat > "$nixp_wrapper" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
exec ${(qqq)REPO_ROOT}/scripts/nix_portable_install.sh --nix "\$@"
EOF

  cat > "$shell_wrapper" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
exec ${(qqq)REPO_ROOT}/scripts/nix_portable_install.sh --shell "\$@"
EOF

  cat > "$run_wrapper" <<EOF
#!/usr/bin/env zsh
set -euo pipefail
exec ${(qqq)REPO_ROOT}/scripts/nix_portable_install.sh --run "\$@"
EOF

  chmod +x "$nixp_wrapper" "$shell_wrapper" "$run_wrapper"
}

parse_args() {
  while (($#)); do
    case "$1" in
      --profile)
        shift
        if ((! $#)); then
          echo "ERROR: --profile requires cli or full" >&2
          return 1
        fi
        PROFILE="$1"
        ;;
      --profile=*)
        PROFILE="${1#--profile=}"
        ;;
      --cli-only)
        PROFILE="cli"
        ;;
      --with-gui-apps)
        PROFILE="full"
        ;;
      --shell)
        MODE="shell"
        ;;
      --run)
        MODE="run"
        shift
        if ((! $#)); then
          echo "ERROR: --run requires a command" >&2
          return 1
        fi
        RUN_ARGS=("$@")
        return
        ;;
      --nix)
        MODE="nix"
        shift
        if ((! $#)); then
          echo "ERROR: --nix requires arguments" >&2
          return 1
        fi
        RUN_ARGS=("$@")
        return
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
    esac
    shift
  done
}

main() {
  local -a RUN_ARGS=()

  parse_args "$@"
  install_nix_portable
  write_wrappers

  case "$MODE" in
    install)
      nix_portable nix --version
      echo "nix-portable is ready."
      echo "Use: $NIX_PORTABLE_BIN_DIR/nixp --version"
      echo "Use: $NIX_PORTABLE_BIN_DIR/dotfiles-nix-shell"
      echo "Use: $NIX_PORTABLE_BIN_DIR/dotfiles-nix-run COMMAND [ARGS...]"
      ;;
    shell)
      enter_package_shell
      ;;
    run)
      run_with_package_set "${RUN_ARGS[@]}"
      ;;
    nix)
      run_raw_nix "${RUN_ARGS[@]}"
      ;;
  esac
}

main "$@"
