#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly LIB_DIR="$SCRIPT_DIR/lib"
readonly DEFAULT_INSTALLER_URL="https://hermes-agent.nousresearch.com/install.sh"

source "$LIB_DIR/setup_profile.sh"
source "$LIB_DIR/runtime.sh"

INSTALLER_URL="${HERMES_INSTALLER_URL:-$DEFAULT_INSTALLER_URL}"
UPDATE_ONLY=0
CHECK_ONLY=0

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

  dotfiles_temporary_directory_root
  temp_root="$REPLY"
  # mkdir is atomic, so a private directory keeps the downloaded installer from
  # being swapped between the download and the execution in a shared /tmp.
  dotfiles_create_unique_temp_directory "$temp_root" "dotfiles-hermes-installer" || return 1
  temp_dir="$REPLY"
  chmod 700 "$temp_dir"
  installer_file="$temp_dir/install.sh"

  log "Downloading the Hermes Agent installer from $INSTALLER_URL"
  if ! curl -fsSL "$INSTALLER_URL" -o "$installer_file"; then
    rm -rf "$temp_dir"
    echo "ERROR: failed to download the Hermes Agent installer from $INSTALLER_URL" >&2
    return 1
  fi

  log "Running the Hermes Agent installer"
  if ! bash "$installer_file"; then
    rm -rf "$temp_dir"
    return 1
  fi
  rm -rf "$temp_dir"
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
