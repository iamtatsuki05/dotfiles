#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DEFAULT_PROFILE_FILE="$XDG_CONFIG_HOME/dotfiles/profile"

source "$LIB_DIR/setup_profile.sh"

REPO_ROOT="$DEFAULT_REPO_ROOT"
MANAGER_FILE="$XDG_CONFIG_HOME/dotfiles/manager"
PROFILE_FILE="$DEFAULT_PROFILE_FILE"
DRY_RUN=0
MARK_DEFAULT=0

usage() {
  cat <<EOF
Usage:
  zsh scripts/chezmoi_apply.sh [--dry-run]
  zsh scripts/chezmoi_apply.sh --mark-default

Options:
  --dry-run            Show the chezmoi apply plan without writing home files.
  --mark-default       Mark chezmoi as the default dotfiles manager for this machine.
  --profile full|cli   Select setup profile for templates.
  --cli-only           Shortcut for --profile cli.
  --full               Shortcut for --profile full.
  --repo-root PATH     Override repository root. Intended for tests.
  --manager-file PATH  Override manager marker path. Intended for tests.
  --profile-file PATH  Override profile marker path. Intended for tests.
  -h, --help           Show this help.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --mark-default)
        MARK_DEFAULT=1
        ;;
      --profile)
        shift
        if ((! $#)); then
          echo "ERROR: --profile requires a value" >&2
          return 1
        fi
        DOTFILES_PROFILE="$1"
        ;;
      --profile=*)
        DOTFILES_PROFILE="${1#--profile=}"
        ;;
      --cli-only)
        DOTFILES_PROFILE="cli"
        ;;
      --full)
        DOTFILES_PROFILE="full"
        ;;
      --repo-root)
        shift
        if ((! $#)); then
          echo "ERROR: --repo-root requires a value" >&2
          return 1
        fi
        REPO_ROOT="$1"
        ;;
      --repo-root=*)
        REPO_ROOT="${1#--repo-root=}"
        ;;
      --manager-file)
        shift
        if ((! $#)); then
          echo "ERROR: --manager-file requires a value" >&2
          return 1
        fi
        MANAGER_FILE="$1"
        ;;
      --manager-file=*)
        MANAGER_FILE="${1#--manager-file=}"
        ;;
      --profile-file)
        shift
        if ((! $#)); then
          echo "ERROR: --profile-file requires a value" >&2
          return 1
        fi
        PROFILE_FILE="$1"
        ;;
      --profile-file=*)
        PROFILE_FILE="${1#--profile-file=}"
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

  REPO_ROOT="$(cd "$REPO_ROOT" && pwd)"

  if [[ -z "$DOTFILES_PROFILE" ]]; then
    if [[ -f "$PROFILE_FILE" ]]; then
      DOTFILES_PROFILE="$(sed -n '1p' "$PROFILE_FILE")"
    else
      DOTFILES_PROFILE="$(dotfiles_default_profile)"
    fi
  fi

  dotfiles_validate_profile "$DOTFILES_PROFILE"
}

has_mise_command() {
  command -v mise >/dev/null 2>&1
}

home_local_chezmoi_command() {
  if [[ -x "$HOME/.local/bin/chezmoi" ]]; then
    print -r -- "$HOME/.local/bin/chezmoi"
    return 0
  fi

  return 1
}

mise_chezmoi_command() {
  local install_dir

  if ! has_mise_command; then
    return 1
  fi

  install_dir="$(mise where chezmoi@latest 2>/dev/null)" || return 1
  if [[ -x "$install_dir/chezmoi" ]]; then
    print -r -- "$install_dir/chezmoi"
    return 0
  fi

  return 1
}

print_chezmoi_missing_error() {
  cat >&2 <<'EOF'
ERROR: chezmoi is not installed.

Install it first, for example:
  sh -c "$(curl -fsLS get.chezmoi.io)" -- -b "$HOME/.local/bin"
  # or: brew install chezmoi
  # or: mise use --global chezmoi@latest
EOF
}

run_chezmoi() {
  local home_chezmoi_bin
  local mise_chezmoi_bin

  if command -v chezmoi >/dev/null 2>&1; then
    DOTFILES_PROFILE="$DOTFILES_PROFILE" DOTFILES_REPO_ROOT="$REPO_ROOT" chezmoi "$@"
    return 0
  fi

  if home_chezmoi_bin="$(home_local_chezmoi_command)"; then
    DOTFILES_PROFILE="$DOTFILES_PROFILE" DOTFILES_REPO_ROOT="$REPO_ROOT" "$home_chezmoi_bin" "$@"
    return 0
  fi

  if mise_chezmoi_bin="$(mise_chezmoi_command)"; then
    DOTFILES_PROFILE="$DOTFILES_PROFILE" DOTFILES_REPO_ROOT="$REPO_ROOT" "$mise_chezmoi_bin" "$@"
    return 0
  fi

  if has_mise_command; then
    DOTFILES_PROFILE="$DOTFILES_PROFILE" DOTFILES_REPO_ROOT="$REPO_ROOT" mise exec chezmoi@latest -- chezmoi "$@"
    return 0
  fi

  print_chezmoi_missing_error
  return 1
}

ensure_chezmoi_source_state() {
  if [[ -f "$REPO_ROOT/.chezmoiroot" && -d "$REPO_ROOT/home" ]]; then
    return 0
  fi

  cat >&2 <<EOF
ERROR: chezmoi source state is not generated in $REPO_ROOT.

Expected:
  .chezmoiroot
  home/
EOF
  return 1
}

mark_default_manager() {
  local manager_dir="${MANAGER_FILE:h}"
  local profile_dir="${PROFILE_FILE:h}"

  mkdir -p "$manager_dir"
  mkdir -p "$profile_dir"
  print -r -- "chezmoi" > "$MANAGER_FILE"
  print -r -- "$DOTFILES_PROFILE" > "$PROFILE_FILE"
  echo "Marked chezmoi as default dotfiles manager: $MANAGER_FILE"
  echo "Saved dotfiles profile: $PROFILE_FILE"
}

apply_chezmoi() {
  if (( DRY_RUN )); then
    run_chezmoi -S "$REPO_ROOT" apply -n -v
  else
    run_chezmoi -S "$REPO_ROOT" apply -v
  fi
}

main() {
  parse_args "$@"
  ensure_chezmoi_source_state
  apply_chezmoi

  if (( MARK_DEFAULT && ! DRY_RUN )); then
    mark_default_manager
  fi
}

main "$@"
