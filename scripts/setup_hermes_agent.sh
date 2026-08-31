#!/usr/bin/env bash

set -euo pipefail

resolve_script_directory() {
  local script_path="$1"
  local script_dir
  local link_target

  if [[ "$script_path" != /* && "$script_path" != */* ]]; then
    script_path="$(command -v "$script_path" 2>/dev/null || printf '%s' "$script_path")"
  fi
  while [[ -L "$script_path" ]]; do
    script_dir="$(cd -P "$(dirname "$script_path")" && pwd)" || return 1
    link_target="$(readlink "$script_path")" || return 1
    if [[ "$link_target" == /* ]]; then
      script_path="$link_target"
    else
      script_path="$script_dir/$link_target"
    fi
  done
  cd -P "$(dirname "$script_path")" && pwd
}

SCRIPT_DIR="$(resolve_script_directory "$0")" || {
  echo "ERROR: failed to resolve setup_hermes_agent.sh directory." >&2
  exit 1
}
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly DEFAULT_INSTALLER_URL="https://hermes-agent.nousresearch.com/install.sh"

detect_host_ostype() {
  local uname_bin
  local kernel_name

  if [[ -x /usr/bin/uname ]]; then
    uname_bin="/usr/bin/uname"
  elif [[ -x /bin/uname ]]; then
    uname_bin="/bin/uname"
  else
    echo "ERROR: uname is required to detect the host OS." >&2
    return 1
  fi
  kernel_name="$("$uname_bin" -s)" || return 1
  case "$kernel_name" in
    Darwin) OSTYPE=darwin ;;
    Linux) OSTYPE=linux ;;
    *)
      echo "ERROR: unsupported host kernel: $kernel_name" >&2
      return 1
      ;;
  esac
}

detect_host_ostype

source "$LIB_DIR/setup_profile.sh"
source "$LIB_DIR/runtime.sh"

INSTALLER_URL="${HERMES_INSTALLER_URL:-$DEFAULT_INSTALLER_URL}"
UPDATE_ONLY=0
CHECK_ONLY=0
HERMES_INSTALLER_TEMP_DIR=""
HERMES_INSTALLER_TEMP_ROOT=""
HERMES_INSTALLER_TEMP_PREFIX=""

cleanup_hermes_installer_temp() {
  local temp_dir="$HERMES_INSTALLER_TEMP_DIR"
  local candidate
  local suffix_index=0

  HERMES_INSTALLER_TEMP_DIR=""
  if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then
    rm -rf "$temp_dir"
  fi
  if [[ -z "$temp_dir" && -n "$HERMES_INSTALLER_TEMP_ROOT" && -n "$HERMES_INSTALLER_TEMP_PREFIX" ]]; then
    while (( suffix_index < 1024 )); do
      candidate="$HERMES_INSTALLER_TEMP_ROOT/$HERMES_INSTALLER_TEMP_PREFIX.$$.$suffix_index"
      if [[ -d "$candidate" && ! -L "$candidate" ]]; then
        rmdir "$candidate" 2>/dev/null || true
      fi
      suffix_index=$((suffix_index + 1))
    done
  fi
  HERMES_INSTALLER_TEMP_ROOT=""
  HERMES_INSTALLER_TEMP_PREFIX=""
}

usage() {
  cat <<EOF
Usage:
  zsh scripts/setup_hermes_agent.sh [options]

Hermes Agent is distributed through its official shell installer, Docker image,
or Hermes Desktop. Upstream removed pip/PyPI and Homebrew distribution, so this
script replaces the former mise pipx entry.

Options:
  --update-only     Update an existing install only. Do nothing when hermes is missing.
  --check           Report whether an update is available without installing anything.
  --installer-url URL
                    Override the installer URL. Defaults to HERMES_INSTALLER_URL or
                    $DEFAULT_INSTALLER_URL.
  -h, --help        Show this help.
EOF
}

log() {
  echo "===> $*"
}

warn() {
  echo "===> $*" >&2
}

parse_args() {
  while (($#)); do
    case "$1" in
      --update-only)
        UPDATE_ONLY=1
        ;;
      --check)
        CHECK_ONLY=1
        ;;
      --installer-url)
        shift
        if ((! $#)); then
          echo "ERROR: --installer-url requires a value" >&2
          return 1
        fi
        INSTALLER_URL="$1"
        ;;
      --installer-url=*)
        INSTALLER_URL="${1#--installer-url=}"
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

resolve_hermes_command() {
  dotfiles_resolve_command_from_path "hermes"
}

update_hermes() {
  local hermes_bin="$1"

  if (( CHECK_ONLY )); then
    log "Checking for a Hermes Agent update"
    "$hermes_bin" update --check
    return 0
  fi

  log "Updating Hermes Agent with hermes update"
  "$hermes_bin" update --yes
}

install_hermes() {
  local temp_root
  local temp_dir
  local installer_file
  local previous_umask
  local temp_dir_status=0

  trap cleanup_hermes_installer_temp EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  dotfiles_temporary_directory_root
  temp_root="$REPLY"
  HERMES_INSTALLER_TEMP_ROOT="$temp_root"
  HERMES_INSTALLER_TEMP_PREFIX="dotfiles-hermes-installer"
  previous_umask="$(umask)"
  umask 077
  if dotfiles_create_unique_temp_directory "$temp_root" "dotfiles-hermes-installer"; then
    temp_dir="$REPLY"
    HERMES_INSTALLER_TEMP_DIR="$temp_dir"
  else
    temp_dir_status="$?"
    HERMES_INSTALLER_TEMP_ROOT=""
    HERMES_INSTALLER_TEMP_PREFIX=""
  fi
  (( temp_dir_status == 0 )) || return "$temp_dir_status"
  if ! chmod 700 "$temp_dir"; then
    return 1
  fi
  if ! installer_file="$(mktemp "$temp_dir/install.XXXXXX")"; then
    return 1
  fi
  if ! chmod 600 "$installer_file"; then
    return 1
  fi
  umask "$previous_umask"

  log "Downloading the Hermes Agent installer from $INSTALLER_URL"
  if ! curl -fsSL "$INSTALLER_URL" -o "$installer_file"; then
    echo "ERROR: failed to download the Hermes Agent installer from $INSTALLER_URL" >&2
    return 1
  fi

  log "Running the Hermes Agent installer"
  if ! bash "$installer_file"; then
    return 1
  fi
  cleanup_hermes_installer_temp
}

main() {
  local hermes_bin

  parse_args "$@"

  if resolve_hermes_command; then
    hermes_bin="$REPLY"
    log "Found Hermes Agent at $hermes_bin"
    update_hermes "$hermes_bin"
    return 0
  fi

  if (( CHECK_ONLY )); then
    warn "Hermes Agent is not installed; nothing to check"
    return 0
  fi

  if (( UPDATE_ONLY )); then
    warn "Hermes Agent is not installed; skipping (run 'mise run hermes-setup' to install it)"
    return 0
  fi

  install_hermes
}

main "$@"
