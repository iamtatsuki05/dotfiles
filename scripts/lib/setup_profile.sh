#!/usr/bin/zsh

readonly DOTFILES_OS_NAME="$(uname -s)"

DOTFILES_PROFILE=""

dotfiles_is_macos() {
  [[ "$DOTFILES_OS_NAME" == "Darwin" ]]
}

dotfiles_default_profile() {
  if dotfiles_is_macos; then
    echo "full"
  else
    echo "cli"
  fi
}

dotfiles_print_profile_usage() {
  local command_name="$1"

  cat <<EOF
Usage:
  zsh $command_name [--profile full|cli]
  zsh $command_name --cli-only

Profiles:
  full  Full macOS setup.
  cli   Portable CLI-focused setup. This is the default on Linux.

Defaults:
  macOS -> full
  Linux -> cli
EOF
}

dotfiles_validate_profile() {
  local profile="$1"

  case "$profile" in
    full|cli)
      ;;
    *)
      echo "ERROR: unknown profile: $profile" >&2
      return 1
      ;;
  esac

  if [[ "$profile" == "full" ]] && ! dotfiles_is_macos; then
    echo "ERROR: full profile is macOS-only. Use --profile cli on $DOTFILES_OS_NAME." >&2
    return 1
  fi
}

dotfiles_parse_profile_args() {
  local command_name="$1"
  shift

  DOTFILES_PROFILE=""

  while (($#)); do
    case "$1" in
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
      -h|--help)
        dotfiles_print_profile_usage "$command_name"
        exit 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        dotfiles_print_profile_usage "$command_name" >&2
        return 1
        ;;
    esac
    shift
  done

  DOTFILES_PROFILE="${DOTFILES_PROFILE:-$(dotfiles_default_profile)}"
  dotfiles_validate_profile "$DOTFILES_PROFILE"
}
