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
  local installed_ids_file="$1"
  local app_id="$2"

  awk -v id="$app_id" '$1 == id { found = 1 } END { exit found ? 0 : 1 }' "$installed_ids_file"
}

progress_label() {
  local current="$1"
  local total="$2"

  printf '[%*d/%d]' "${#total}" "$current" "$total"
}

install_mas_app() {
  local mas_cmd="$1"
  local installed_ids_file="$2"
  local current="$3"
  local total="$4"
  local app_name="$5"
  local app_id="$6"
  local output_file
  local prefix

  prefix="$(progress_label "$current" "$total")"
  if mas_id_is_installed "$installed_ids_file" "$app_id"; then
    echo "$prefix Using $app_name ($app_id)"
    REPLY="used"
    return 0
  fi

  echo "$prefix Installing $app_name ($app_id)"
  output_file="$(mktemp "${TMPDIR:-/tmp}/dotfiles-mas-install.XXXXXX")"
  if ${(z)mas_cmd} install "$app_id" > "$output_file" 2>&1; then
    echo "$prefix Installed $app_name"
    rm -f "$output_file"
    REPLY="installed"
    return 0
  fi

  warn "$prefix Installing $app_name ($app_id) failed; continuing."
  sed 's/^/  /' "$output_file" >&2
  rm -f "$output_file"
  REPLY="failed"
  return 0
}

install_mas_apps() {
  local mas_cmd
  local apps_file
  local installed_ids_file
  local app_name
  local app_id
  local total_apps
  local current_app=0
  local used_count=0
  local installed_count=0
  local failed_count=0

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

  installed_ids_file="$(mktemp "${TMPDIR:-/tmp}/dotfiles-mas-installed.XXXXXX")"
  if ! ${(z)mas_cmd} list > "$installed_ids_file" 2>/dev/null; then
    warn "failed to read installed Mac App Store apps; attempting installs anyway"
    : > "$installed_ids_file"
  fi

  total_apps="$(wc -l < "$apps_file" | tr -d '[:space:]')"
  log "Installing Mac App Store apps best-effort ($total_apps apps)"
  while IFS=$'\t' read -r app_name app_id || [[ -n "$app_name$app_id" ]]; do
    [[ -n "$app_name" && -n "$app_id" ]] || continue
    current_app=$((current_app + 1))
    install_mas_app "$mas_cmd" "$installed_ids_file" "$current_app" "$total_apps" "$app_name" "$app_id"
    case "$REPLY" in
      used)
        used_count=$((used_count + 1))
        ;;
      installed)
        installed_count=$((installed_count + 1))
        ;;
      failed)
        failed_count=$((failed_count + 1))
        ;;
    esac
  done < "$apps_file"

  rm -f "$apps_file"
  rm -f "$installed_ids_file"
  log "Mac App Store app step complete: used=$used_count installed=$installed_count failed=$failed_count"
}

main() {
  parse_args "$@"
  install_mas_apps
}

main "$@"
