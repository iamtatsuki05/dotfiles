#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly DOTFILES_DIR="$REPO_ROOT/dotfiles"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly PROFILE_FILE="$XDG_CONFIG_HOME/dotfiles/profile"
readonly MANAGER_FILE="$XDG_CONFIG_HOME/dotfiles/manager"

source "$LIB_DIR/setup_profile.sh"

FROM_HOOK=0

log() {
  echo "===> $*"
}

parse_args() {
  local args=()
  local has_profile=0

  while (($#)); do
    case "$1" in
      --from-hook)
        FROM_HOOK=1
        ;;
      --profile|--profile=*|--cli-only|--full)
        has_profile=1
        args+=("$1")
        ;;
      *)
        args+=("$1")
        ;;
    esac
    shift
  done

  if [[ "$has_profile" == "0" && -f "$PROFILE_FILE" ]]; then
    args=(--profile "$(sed -n '1p' "$PROFILE_FILE")" "${args[@]}")
  fi

  dotfiles_parse_profile_args "scripts/apply_updates.sh" "${args[@]}"
}

copy_dotfiles() {
  local profile="$1"

  log "Syncing dotfiles"
  cp -r "$DOTFILES_DIR"/. ~/
}

current_dotfiles_manager() {
  if [[ -f "$MANAGER_FILE" ]]; then
    sed -n '1p' "$MANAGER_FILE"
    return
  fi

  echo "legacy"
}

sync_home_state() {
  local profile="$1"
  local manager
  manager="$(current_dotfiles_manager)"

  if [[ "$manager" == "chezmoi" ]]; then
    if [[ -f "$REPO_ROOT/.chezmoiroot" ]]; then
      log "Applying chezmoi source state"
      if zsh "$SCRIPT_DIR/chezmoi_apply.sh" --profile "$profile"; then
        return
      fi
    fi

    echo "WARNING: chezmoi manager is selected but unavailable; falling back to legacy dotfiles copy" >&2
  fi

  copy_dotfiles "$profile"
}

sync_agent_files() {
  if [[ ! -f "$DOTFILES_DIR/.agent/sync.sh" ]]; then
    return 0
  fi

  log "Syncing agent files"
  zsh "$DOTFILES_DIR/.agent/sync.sh"
}

sync_configs() {
  log "Syncing application configs"
  zsh "$SCRIPT_DIR/setup_config.sh"
}

sync_cron_if_needed() {
  local profile="$1"

  if [[ "$profile" != "full" ]]; then
    return 0
  fi

  log "Syncing cron"
  zsh "$SCRIPT_DIR/setup_cron.sh"
}

refresh_git_hooks() {
  local profile="$1"

  log "Refreshing git hooks"
  if ! zsh "$SCRIPT_DIR/setup_git_hooks.sh" --profile "$profile"; then
    echo "WARNING: failed to refresh git hooks" >&2
  fi
}

main() {
  parse_args "$@"

  local profile="$DOTFILES_PROFILE"
  log "Applying dotfiles updates (profile: $profile)"

  sync_home_state "$profile"
  sync_agent_files
  sync_configs
  sync_cron_if_needed "$profile"
  refresh_git_hooks "$profile"

  if [[ "$FROM_HOOK" == "1" ]]; then
    log "Dotfiles hook update complete"
  else
    log "Dotfiles update complete"
  fi
}

main "$@"
