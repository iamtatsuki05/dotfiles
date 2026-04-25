#!/usr/bin/zsh

set -euo pipefail

readonly NIX_USER_CHROOT_VERSION="${NIX_USER_CHROOT_VERSION:-2.1.1}"
readonly NIX_USER_CHROOT_BASE_URL="https://github.com/nix-community/nix-user-chroot/releases/download/$NIX_USER_CHROOT_VERSION"
readonly ROOTLESS_NIX_DIR="${ROOTLESS_NIX_DIR:-$HOME/.nix}"
readonly ROOTLESS_NIX_BIN_DIR="${ROOTLESS_NIX_BIN_DIR:-$HOME/.local/bin}"
readonly NIX_USER_CHROOT_BIN="$ROOTLESS_NIX_BIN_DIR/nix-user-chroot"
readonly NIX_CONF_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/nix/nix.conf"

RUN_COMMAND=0
SHELL_AFTER_INSTALL=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/nix_rootless_install.sh
  zsh scripts/nix_rootless_install.sh --run COMMAND [ARGS...]
  zsh scripts/nix_rootless_install.sh --shell

Options:
  --run COMMAND [ARGS...]  Install rootless Nix if needed, then run COMMAND inside nix-user-chroot.
  --shell                  Install rootless Nix if needed, then enter a Nix-enabled login shell.
  -h, --help               Show this help.

This is for Linux hosts where sudo and /nix are unavailable.
It installs Nix under \$HOME/.nix through nix-user-chroot.
EOF
}

system_asset() {
  case "$(uname -s):$(uname -m)" in
    Linux:x86_64|Linux:amd64)
      print -r -- "nix-user-chroot-bin-$NIX_USER_CHROOT_VERSION-x86_64-unknown-linux-musl"
      ;;
    Linux:aarch64|Linux:arm64)
      print -r -- "nix-user-chroot-bin-$NIX_USER_CHROOT_VERSION-aarch64-unknown-linux-musl"
      ;;
    *)
      echo "ERROR: rootless Nix is only supported here on Linux x86_64/aarch64." >&2
      return 1
      ;;
  esac
}

check_user_namespaces() {
  if ! command -v unshare >/dev/null 2>&1; then
    echo "ERROR: unshare is required for nix-user-chroot." >&2
    return 1
  fi

  if ! unshare --user --pid true >/dev/null 2>&1; then
    echo "ERROR: unprivileged user namespaces are not available on this host." >&2
    return 1
  fi
}

install_nix_user_chroot() {
  local asset
  local url

  if [[ -x "$NIX_USER_CHROOT_BIN" ]]; then
    return 0
  fi

  asset="$(system_asset)"
  url="$NIX_USER_CHROOT_BASE_URL/$asset"

  mkdir -p "$ROOTLESS_NIX_BIN_DIR"
  curl -fL "$url" -o "$NIX_USER_CHROOT_BIN"
  chmod +x "$NIX_USER_CHROOT_BIN"
}

configure_nix() {
  mkdir -p "${NIX_CONF_FILE:h}"

  if [[ -f "$NIX_CONF_FILE" ]] && grep -q '^experimental-features = ' "$NIX_CONF_FILE"; then
    sed -i.bak 's/^experimental-features = .*/experimental-features = nix-command flakes/' "$NIX_CONF_FILE"
  else
    print -r -- 'experimental-features = nix-command flakes' >> "$NIX_CONF_FILE"
  fi
}

rootless_exec() {
  "$NIX_USER_CHROOT_BIN" "$ROOTLESS_NIX_DIR" bash -lc '
    set -euo pipefail
    if [[ -r "$HOME/.nix-profile/etc/profile.d/nix.sh" ]]; then
      . "$HOME/.nix-profile/etc/profile.d/nix.sh"
    fi
    exec "$@"
  ' nix-rootless "$@"
}

rootless_nix_available() {
  [[ -x "$NIX_USER_CHROOT_BIN" ]] || return 1
  rootless_exec nix --version >/dev/null 2>&1
}

install_nix() {
  mkdir -p "$ROOTLESS_NIX_DIR"

  if rootless_nix_available; then
    return 0
  fi

  "$NIX_USER_CHROOT_BIN" "$ROOTLESS_NIX_DIR" bash -lc \
    'curl -L https://nixos.org/nix/install | sh -s -- --no-daemon'
}

write_wrappers() {
  local nix_wrapper="$ROOTLESS_NIX_BIN_DIR/nix-rootless"
  local shell_wrapper="$ROOTLESS_NIX_BIN_DIR/rootless-nix-shell"

  mkdir -p "$ROOTLESS_NIX_BIN_DIR"

  cat > "$nix_wrapper" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
exec "${NIX_USER_CHROOT_BIN:-$HOME/.local/bin/nix-user-chroot}" "${ROOTLESS_NIX_DIR:-$HOME/.nix}" bash -lc '
  set -euo pipefail
  . "$HOME/.nix-profile/etc/profile.d/nix.sh"
  exec nix "$@"
' nix-rootless "$@"
EOF

  cat > "$shell_wrapper" <<'EOF'
#!/usr/bin/env zsh
set -euo pipefail
readonly login_shell="${ROOTLESS_NIX_LOGIN_SHELL:-/usr/bin/zsh}"
exec "${NIX_USER_CHROOT_BIN:-$HOME/.local/bin/nix-user-chroot}" "${ROOTLESS_NIX_DIR:-$HOME/.nix}" bash -lc '
  set -euo pipefail
  . "$HOME/.nix-profile/etc/profile.d/nix.sh"
  exec "$@"
' rootless-nix-shell "$login_shell" -l
EOF

  chmod +x "$nix_wrapper" "$shell_wrapper"
}

main() {
  local -a command_args=()

  while (($#)); do
    case "$1" in
      --run)
        RUN_COMMAND=1
        shift
        command_args=("$@")
        break
        ;;
      --shell)
        SHELL_AFTER_INSTALL=1
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

  if (( RUN_COMMAND && ${#command_args[@]} == 0 )); then
    echo "ERROR: --run requires a command" >&2
    return 1
  fi

  check_user_namespaces
  install_nix_user_chroot
  configure_nix
  install_nix
  write_wrappers

  if (( RUN_COMMAND )); then
    rootless_exec "${command_args[@]}"
    return
  fi

  if (( SHELL_AFTER_INSTALL )); then
    exec "$ROOTLESS_NIX_BIN_DIR/rootless-nix-shell"
  fi

  echo "Rootless Nix is ready."
  echo "Use: $ROOTLESS_NIX_BIN_DIR/nix-rootless --version"
  echo "Use: $ROOTLESS_NIX_BIN_DIR/rootless-nix-shell"
}

main "$@"
