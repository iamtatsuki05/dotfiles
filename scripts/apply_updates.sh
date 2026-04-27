#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly PROFILE_FILE="$XDG_CONFIG_HOME/dotfiles/profile"

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

apply_chezmoi() {
  local profile="$1"

  log "Applying chezmoi source state"
  zsh "$SCRIPT_DIR/chezmoi_apply.sh" --profile "$profile"
}

sync_agent_files() {
  if [[ ! -f "$SCRIPT_DIR/setup_agent_files.sh" ]]; then
    return 0
  fi

  log "Syncing agent files"
  zsh "$SCRIPT_DIR/setup_agent_files.sh"
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

  apply_chezmoi "$profile"
  sync_agent_files
  refresh_git_hooks "$profile"

  if [[ "$FROM_HOOK" == "1" ]]; then
    log "Dotfiles hook update complete"
  else
    log "Dotfiles update complete"
  fi
}

main "$@"
