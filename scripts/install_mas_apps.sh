#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly MAS_APPS_CONFIG="$REPO_ROOT/config/nix/mas-apps.nix"

source "$LIB_DIR/setup_profile.sh"

PROFILE=""

usage() {
  cat <<EOF
Usage:
  zsh scripts/install_mas_apps.sh [--profile full|cli]
  zsh scripts/install_mas_apps.sh --cli-only

Installs configured Mac App Store apps best-effort. Individual failures are
reported but do not fail the setup run.
EOF
}

log() {
  echo "===> $*"
}

warn() {
  echo "WARNING: $*" >&2
}

parse_args() {
  local profile_args=()

  while (($#)); do
    case "$1" in
      --profile|--profile=*|--cli-only|--full)
        profile_args+=("$1")
        if [[ "$1" == "--profile" ]]; then
          shift
          if ((! $#)); then
            echo "ERROR: --profile requires full or cli" >&2
            return 1
          fi
          profile_args+=("$1")
        fi
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

  dotfiles_parse_profile_args "scripts/install_mas_apps.sh" "${profile_args[@]}"
  PROFILE="$DOTFILES_PROFILE"
}

nix_command() {
  if command -v nix >/dev/null 2>&1; then
    command -v nix
    return 0
  fi

  warn "nix is not available; skipping Mac App Store apps"
  return 1
}

mas_command() {
  if command -v mas >/dev/null 2>&1; then
    command -v mas
    return 0
  fi

  local nix_bin
  nix_bin="$(nix_command)" || return 1
  print -r -- "$nix_bin shell $REPO_ROOT#dotfiles-full-packages -c mas"
}

mas_apps_tsv() {
  local nix_bin
  local expr

  [[ -f "$MAS_APPS_CONFIG" ]] || return 0

  nix_bin="$(nix_command)" || return 1
  expr='let apps = import '"$MAS_APPS_CONFIG"'; in builtins.concatStringsSep "\n" (map (name: name + "\t" + builtins.toString apps.${name}) (builtins.attrNames apps))'
  "$nix_bin" eval --raw --impure --extra-experimental-features "nix-command flakes" --expr "$expr"
}

mas_id_is_installed() {
  local mas_cmd="$1"
  local app_id="$2"

  ${(z)mas_cmd} list 2>/dev/null | awk -v id="$app_id" '$1 == id { found = 1 } END { exit found ? 0 : 1 }'
}

install_mas_app() {
  local mas_cmd="$1"
  local app_name="$2"
  local app_id="$3"
  local output_file

  if mas_id_is_installed "$mas_cmd" "$app_id"; then
    echo "Using $app_name"
    return 0
  fi

  echo "Installing $app_name"
  output_file="$(mktemp "${TMPDIR:-/tmp}/dotfiles-mas-install.XXXXXX")"
  if ${(z)mas_cmd} install "$app_id" > "$output_file" 2>&1; then
    rm -f "$output_file"
    return 0
  fi

  warn "Installing $app_name ($app_id) failed; continuing."
  sed 's/^/  /' "$output_file" >&2
  rm -f "$output_file"
  return 0
}

install_mas_apps() {
  local mas_cmd
  local apps_file
  local app_name
  local app_id

  if ! dotfiles_is_macos; then
    log "Skipping Mac App Store apps because this host is not macOS"
    return 0
  fi

  if [[ "$PROFILE" != "full" ]]; then
    log "Skipping Mac App Store apps for cli profile"
    return 0
  fi

  if [[ "${DOTFILES_SKIP_MAS_APPS:-0}" == "1" ]]; then
    log "Skipping Mac App Store apps because DOTFILES_SKIP_MAS_APPS=1"
    return 0
  fi

  mas_cmd="$(mas_command)" || return 0
  apps_file="$(mktemp "${TMPDIR:-/tmp}/dotfiles-mas-apps.XXXXXX")"
  if ! mas_apps_tsv > "$apps_file"; then
    warn "failed to read $MAS_APPS_CONFIG; skipping Mac App Store apps"
    rm -f "$apps_file"
    return 0
  fi

  if [[ ! -s "$apps_file" ]]; then
    rm -f "$apps_file"
    log "No Mac App Store apps configured"
    return 0
  fi

  log "Installing Mac App Store apps best-effort"
  while IFS=$'\t' read -r app_name app_id || [[ -n "$app_name$app_id" ]]; do
    [[ -n "$app_name" && -n "$app_id" ]] || continue
    install_mas_app "$mas_cmd" "$app_name" "$app_id"
  done < "$apps_file"

  rm -f "$apps_file"
  log "Mac App Store app step complete"
}

main() {
  parse_args "$@"
  install_mas_apps
}

main "$@"
