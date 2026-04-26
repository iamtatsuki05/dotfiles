#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly MISE_CONFIG_FILE="$REPO_ROOT/config/mise/config.toml"
readonly MISE_TEMPLATE_FILE="$REPO_ROOT/home/.chezmoitemplates/mise-config.toml"
readonly HOMEBREW_FALLBACK_CONFIG="$REPO_ROOT/config/nix/homebrew-fallback.nix"
readonly MAS_APPS_CONFIG="$REPO_ROOT/config/nix/mas-apps.nix"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
TEMP_PKG_CONFIG_SHIM_DIR=""

source "$LIB_DIR/setup_profile.sh"
source "$LIB_DIR/homebrew.sh"

SELECTED_SHELL="zsh"
INSTALL_GUI_APPS=0
UPDATE_SCOPE="all"
NIX_INPUT="all"
SHOW_PROGRESS=0
TOTAL_STEPS=0
CURRENT_STEP=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/update_managed_versions.sh [--profile full|cli] [options]
  zsh scripts/update_managed_versions.sh --cli-only

Options:
  --shell zsh|bash        Shell to use for repository helper scripts. Default: zsh.
  --profile full|cli      Select setup profile. macOS defaults to full; Linux defaults to cli.
  --cli-only              Alias for --profile cli.
  --only all|lock|nix|mise
                          Limit the update flow. Default: all.
  --nix-input all|nixpkgs|home-manager|nix-darwin
                          Limit the flake input updated by the Nix step. Default: all.
  --with-gui-apps         Include GUI apps when applying the updated Nix profile.
  -h, --help              Show this help.

Default flow:
  1. updates flake.lock with nix flake update
  2. applies the updated Nix configuration
  3. syncs home/.chezmoitemplates/mise-config.toml and ~/.config/mise/config.toml
  4. upgrades mise-managed tools within the configured release lines

Use --only lock to update flake.lock only.
Use --only nix to update flake.lock and apply Nix only.
Use --only mise to sync mise config and upgrade mise-managed tools only.
Use --nix-input nixpkgs to update only nixpkgs before applying.
EOF
}

log() {
  echo "===> $*"
}

warn() {
  echo "===> $*" >&2
}

render_progress_bar() {
  local step="$1"
  local total="$2"
  local label="$3"
  local width=28
  local completed=$((step - 1))
  local filled=$((completed * width / total))
  local next_filled=$((step * width / total))
  local active=$((next_filled - filled))
  local remaining
  local done_bar
  local active_bar
  local pending_bar
  local percent=$((completed * 100 / total))

  if ((active < 1)); then
    active=1
  fi

  remaining=$((width - filled - active))
  if ((remaining < 0)); then
    remaining=0
  fi

  done_bar="$(printf '%*s' "$filled" '' | tr ' ' '#')"
  active_bar="$(printf '%*s' "$active" '' | tr ' ' '>')"
  pending_bar="$(printf '%*s' "$remaining" '' | tr ' ' '-')"

  printf '===> [%s%s%s] %d/%d %s (%d%%)\n' \
    "$done_bar" "$active_bar" "$pending_bar" "$step" "$total" "$label" "$percent"
}

start_step() {
  local label="$1"

  CURRENT_STEP=$((CURRENT_STEP + 1))
  if [[ "$SHOW_PROGRESS" == "1" ]]; then
    render_progress_bar "$CURRENT_STEP" "$TOTAL_STEPS" "$label"
  else
    log "$label"
  fi
}

finish_progress() {
  local width=28
  local bar

  if [[ "$SHOW_PROGRESS" != "1" ]]; then
    return 0
  fi

  bar="$(printf '%*s' "$width" '' | tr ' ' '#')"
  printf '===> [%s] %d/%d Managed version update complete (100%%)\n' \
    "$bar" "$TOTAL_STEPS" "$TOTAL_STEPS"
}

mise_command() {
  if command -v mise >/dev/null 2>&1; then
    command -v mise
    return 0
  fi

  echo "ERROR: mise is not installed or not found in PATH" >&2
  return 1
}

nix_command() {
  if command -v nix >/dev/null 2>&1; then
    command -v nix
    return 0
  fi

  echo "ERROR: nix is not installed or not found in PATH" >&2
  return 1
}

run_repo_script() {
  local script_name="$1"
  shift

  "$SELECTED_SHELL" "$SCRIPT_DIR/$script_name" "$@"
}

homebrew_command_exists() {
  dotfiles_has_homebrew
}

list_setting_has_entries() {
  local file_path="$1"
  local setting_name="$2"

  [[ -f "$file_path" ]] || return 1
  awk -v target="$setting_name" '
    BEGIN { in_section = 0 }
    $0 ~ "^[[:space:]]*" target "[[:space:]]*=" { in_section = 1; next }
    in_section && /^[[:space:]]*[A-Za-z0-9_]+[[:space:]]*=/ { in_section = 0 }
    in_section && /^[[:space:]]*"[^"]+"/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$file_path"
}

homebrew_fallback_has_cli_entries() {
  list_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "brews"
}

mas_apps_has_entries() {
  [[ -f "$MAS_APPS_CONFIG" ]] || return 1
  grep -Eq '^[[:space:]]*("[^"]+"|[A-Za-z_][A-Za-z0-9_-]*)[[:space:]]*=' "$MAS_APPS_CONFIG"
}

homebrew_fallback_has_gui_entries() {
  list_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "casks" \
    || list_setting_has_entries "$HOMEBREW_FALLBACK_CONFIG" "vscode" \
    || mas_apps_has_entries
}

homebrew_is_required_for_profile() {
  local profile_name="$1"

  dotfiles_is_macos || return 1

  if homebrew_fallback_has_cli_entries; then
    return 0
  fi

  [[ "$profile_name" == "full" ]] && homebrew_fallback_has_gui_entries
}

prepend_path_if_dir() {
  local dir="$1"

  [[ -d "$dir" ]] || return 0
  case ":$PATH:" in
    *":$dir:"*)
      ;;
    *)
      PATH="$dir:$PATH"
      export PATH
      ;;
  esac
}

prepend_env_dir() {
  local var_name="$1"
  local dir="$2"
  local current

  [[ -d "$dir" ]] || return 0
  eval "current=\${$var_name:-}"
  case ":$current:" in
    *":$dir:"*)
      return 0
      ;;
  esac

  if [[ -n "$current" ]]; then
    eval "export $var_name=\"$dir:$current\""
  else
    eval "export $var_name=\"$dir\""
  fi
}

prepend_flag_path() {
  local var_name="$1"
  local flag_prefix="$2"
  local dir="$3"
  local current
  local flag="$flag_prefix$dir"

  [[ -d "$dir" ]] || return 0
  eval "current=\${$var_name:-}"
  case " $current " in
    *" $flag "*)
      return 0
      ;;
  esac

  if [[ -n "$current" ]]; then
    eval "export $var_name=\"$flag $current\""
  else
    eval "export $var_name=\"$flag\""
  fi
}

cleanup_temporary_dirs() {
  if [[ -n "$TEMP_PKG_CONFIG_SHIM_DIR" && -d "$TEMP_PKG_CONFIG_SHIM_DIR" ]]; then
    rm -rf "$TEMP_PKG_CONFIG_SHIM_DIR"
  fi
}

resolve_nix_apply_profile() {
  local profile_name="$DOTFILES_PROFILE"

  if ! dotfiles_is_macos || homebrew_command_exists || ! homebrew_is_required_for_profile "$profile_name"; then
    print -r -- "$profile_name"
    return 0
  fi

  if homebrew_fallback_has_cli_entries; then
    echo "ERROR: Homebrew is not installed, and config/nix/homebrew-fallback.nix still has brew entries required even for the CLI profile." >&2
    echo "Install Homebrew first, or migrate those fallback brews out of config/nix/homebrew-fallback.nix." >&2
    return 1
  fi

  if [[ "$INSTALL_GUI_APPS" == "1" ]]; then
    echo "ERROR: --with-gui-apps requires Homebrew on this macOS setup because GUI fallback entries are still configured." >&2
    return 1
  fi

  if [[ "$profile_name" == "full" ]] && homebrew_fallback_has_gui_entries; then
    warn "Homebrew is not installed; falling back to the CLI Nix profile for this managed update. Homebrew-managed GUI fallback apps will not be updated."
    print -r -- "cli"
    return 0
  fi

  print -r -- "$profile_name"
}

activate_nix_environment() {
  local hm_vars

  prepend_path_if_dir "$HOME/.nix-profile/bin"
  prepend_path_if_dir "/etc/profiles/per-user/$USER/bin"
  prepend_path_if_dir "/run/current-system/sw/bin"
  prepend_path_if_dir "/nix/var/nix/profiles/default/bin"

  for hm_vars in \
    "$HOME/.nix-profile/etc/profile.d/hm-session-vars.sh" \
    "/etc/profiles/per-user/$USER/etc/profile.d/hm-session-vars.sh"
  do
    if [[ -r "$hm_vars" ]]; then
      source "$hm_vars"
    fi
  done

  local pkg_prefix
  for pkg_prefix in \
    "$HOME/.nix-profile" \
    "/etc/profiles/per-user/$USER" \
    "/run/current-system/sw"
  do
    prepend_env_dir PKG_CONFIG_PATH "$pkg_prefix/lib/pkgconfig"
    prepend_env_dir PKG_CONFIG_PATH "$pkg_prefix/share/pkgconfig"
    prepend_flag_path CPPFLAGS "-I" "$pkg_prefix/include"
    prepend_flag_path LDFLAGS "-L" "$pkg_prefix/lib"
    prepend_env_dir LIBRARY_PATH "$pkg_prefix/lib"
    prepend_env_dir C_INCLUDE_PATH "$pkg_prefix/include"
    prepend_env_dir CPLUS_INCLUDE_PATH "$pkg_prefix/include"
  done

  if ! command -v pkg-config >/dev/null 2>&1 && command -v pkgconf >/dev/null 2>&1; then
    TEMP_PKG_CONFIG_SHIM_DIR="$(mktemp -d)"
    ln -sf "$(command -v pkgconf)" "$TEMP_PKG_CONFIG_SHIM_DIR/pkg-config"
    prepend_path_if_dir "$TEMP_PKG_CONFIG_SHIM_DIR"
  fi
}

parse_args() {
  local profile_args=()

  while (($#)); do
    case "$1" in
      --shell)
        shift
        if ((! $#)); then
          echo "ERROR: --shell requires zsh or bash" >&2
          return 1
        fi
        SELECTED_SHELL="$1"
        ;;
      --shell=*)
        SELECTED_SHELL="${1#--shell=}"
        ;;
      --profile)
        profile_args+=("$1")
        shift
        if ((! $#)); then
          echo "ERROR: --profile requires full or cli" >&2
          return 1
        fi
        profile_args+=("$1")
        ;;
      --profile=*|--cli-only|--full)
        profile_args+=("$1")
        ;;
      --only)
        shift
        if ((! $#)); then
          echo "ERROR: --only requires all, lock, nix, or mise" >&2
          return 1
        fi
        UPDATE_SCOPE="$1"
        ;;
      --only=*)
        UPDATE_SCOPE="${1#--only=}"
        ;;
      --nix-input)
        shift
        if ((! $#)); then
          echo "ERROR: --nix-input requires all, nixpkgs, home-manager, or nix-darwin" >&2
          return 1
        fi
        NIX_INPUT="$1"
        ;;
      --nix-input=*)
        NIX_INPUT="${1#--nix-input=}"
        ;;
      --with-gui-apps)
        INSTALL_GUI_APPS=1
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

  case "$SELECTED_SHELL" in
    zsh|bash)
      ;;
    *)
      echo "ERROR: unsupported shell: $SELECTED_SHELL" >&2
      echo "Choose one of: zsh, bash" >&2
      return 1
      ;;
  esac

  if ! command -v "$SELECTED_SHELL" >/dev/null 2>&1; then
    echo "ERROR: shell not found in PATH: $SELECTED_SHELL" >&2
    return 1
  fi

  case "$UPDATE_SCOPE" in
    all|lock|nix|mise)
      ;;
    *)
      echo "ERROR: unsupported update scope: $UPDATE_SCOPE" >&2
      echo "Choose one of: all, lock, nix, mise" >&2
      return 1
      ;;
  esac

  case "$NIX_INPUT" in
    all|nixpkgs|home-manager|nix-darwin)
      ;;
    *)
      echo "ERROR: unsupported nix input: $NIX_INPUT" >&2
      echo "Choose one of: all, nixpkgs, home-manager, nix-darwin" >&2
      return 1
      ;;
  esac

  if [[ "$UPDATE_SCOPE" == "mise" && "$NIX_INPUT" != "all" ]]; then
    echo "ERROR: --nix-input cannot be used with --only mise" >&2
    return 1
  fi

  dotfiles_parse_profile_args "scripts/update_managed_versions.sh" "${profile_args[@]}"
}

update_mise_versions() {
  cleanup_stale_java_install_state
  MISE_GLOBAL_CONFIG_FILE="$MISE_CONFIG_FILE" "$(mise_command)" upgrade
}

sync_mise_templates() {
  cp "$MISE_CONFIG_FILE" "$MISE_TEMPLATE_FILE"
}

sync_home_mise_config() {
  local target_file="$XDG_CONFIG_HOME/mise/config.toml"
  local target_dir="${target_file%/*}"
  local tmp
  local line

  mkdir -p "$target_dir"
  tmp="$(mktemp)"

  while IFS= read -r line || [[ -n "$line" ]]; do
    printf '%s\n' "${line//__DOTFILES_REPO_ROOT__/$REPO_ROOT}"
  done < "$MISE_CONFIG_FILE" > "$tmp"

  mv "$tmp" "$target_file"
}

cleanup_stale_java_install_state() {
  local java_root="$HOME/.local/share/mise/installs/java"
  local contents_path

  [[ -d "$java_root" ]] || return 0

  while IFS= read -r contents_path; do
    [[ "$(basename "$(dirname "$contents_path")")" == zulu-* ]] || continue
    if [[ -d "$contents_path" && ! -L "$contents_path" ]]; then
      log "Removing stale Java app bundle directory: $contents_path"
      rm -rf "$contents_path"
    fi
  done < <(find "$java_root" -mindepth 2 -maxdepth 2 -type d -name Contents 2>/dev/null)
}

describe_nix_input() {
  case "$NIX_INPUT" in
    all)
      printf '%s\n' "all flake inputs"
      ;;
    *)
      printf '%s\n' "$NIX_INPUT"
      ;;
  esac
}

update_nix_lockfile() {
  local nix_bin
  nix_bin="$(nix_command)"

  (
    cd "$REPO_ROOT"
    if [[ "$NIX_INPUT" == "all" ]]; then
      "$nix_bin" flake update
    else
      # Equivalent to: nix flake lock --update-input <input>
      "$nix_bin" flake lock --update-input "$NIX_INPUT"
    fi
  )
}

apply_nix_configuration() {
  local nix_apply_profile
  local args

  nix_apply_profile="$(resolve_nix_apply_profile)"
  args=("--profile" "$nix_apply_profile")

  if [[ "$INSTALL_GUI_APPS" == "1" ]]; then
    args+=("--with-gui-apps")
  fi

  run_repo_script "nix_install.sh" "${args[@]}"
}

run_mise_update_flow() {
  activate_nix_environment
  start_step "Syncing tracked mise templates"
  sync_mise_templates
  start_step "Syncing ~/.config/mise/config.toml"
  sync_home_mise_config
  start_step "Upgrading managed mise tools within the configured release lines"
  update_mise_versions
}

initialize_progress() {
  SHOW_PROGRESS="${DOTFILES_SHOW_PROGRESS:-1}"

  case "$UPDATE_SCOPE" in
    lock)
      TOTAL_STEPS=1
      ;;
    nix)
      TOTAL_STEPS=2
      ;;
    mise)
      TOTAL_STEPS=3
      ;;
    all)
      TOTAL_STEPS=5
      ;;
  esac
}

main() {
  parse_args "$@"
  trap cleanup_temporary_dirs EXIT
  initialize_progress

  log "Updating managed versions (scope: $UPDATE_SCOPE, nix-input: $(describe_nix_input), profile: $DOTFILES_PROFILE, shell: $SELECTED_SHELL)"

  case "$UPDATE_SCOPE" in
    lock)
      start_step "Updating flake.lock ($(describe_nix_input))"
      update_nix_lockfile
      ;;
    nix)
      start_step "Updating flake.lock ($(describe_nix_input))"
      update_nix_lockfile
      start_step "Applying updated Nix configuration"
      apply_nix_configuration
      ;;
    mise)
      run_mise_update_flow
      ;;
    all)
      start_step "Updating flake.lock ($(describe_nix_input))"
      update_nix_lockfile
      start_step "Applying updated Nix configuration"
      apply_nix_configuration
      run_mise_update_flow
      ;;
  esac

  finish_progress
  log "Managed version update complete"
}

main "$@"
