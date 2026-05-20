#!/usr/bin/env zsh

set -euo pipefail

readonly DEFAULT_BASE_URL="https://cdn.kimi.com/webbridge"
readonly DEFAULT_VERSION="v1.9.10"
BASE_URL="${KIMI_WEBBRIDGE_BASE_URL:-$DEFAULT_BASE_URL}"
VERSION="${KIMI_WEBBRIDGE_VERSION:-$DEFAULT_VERSION}"
INSTALL_DIR="${KIMI_WEBBRIDGE_INSTALL_DIR:-$HOME/.kimi-webbridge}"
NO_START=0
NO_SKILL=0
SYSTEM_SKILLS_BACKUP=""

usage() {
  cat <<EOF
Usage:
  zsh scripts/setup_kimi_webbridge.sh [options]

Options:
  --version VERSION   Install a specific version. Defaults to KIMI_WEBBRIDGE_VERSION or $DEFAULT_VERSION.
  --install-dir DIR   Install directory. Defaults to KIMI_WEBBRIDGE_INSTALL_DIR or ~/.kimi-webbridge.
  --no-start          Install the binary but do not start the daemon.
  --no-skill          Install the binary but skip agent skill installation.
  -h, --help          Show this help.
EOF
}

log() {
  echo "===> $*"
}

warn() {
  echo "WARNING: $*" >&2
}

parse_args() {
  while (($#)); do
    case "$1" in
      --version)
        shift
        if ((! $#)); then
          echo "ERROR: --version requires a value" >&2
          return 1
        fi
        VERSION="$1"
        ;;
      --version=*)
        VERSION="${1#--version=}"
        ;;
      --install-dir)
        shift
        if ((! $#)); then
          echo "ERROR: --install-dir requires a value" >&2
          return 1
        fi
        INSTALL_DIR="$1"
        ;;
      --install-dir=*)
        INSTALL_DIR="${1#--install-dir=}"
        ;;
      --no-start)
        NO_START=1
        ;;
      --no-skill)
        NO_SKILL=1
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

require_command() {
  local command_name="$1"

  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $command_name" >&2
    return 1
  fi
}

detect_platform() {
  local os_name
  local arch_name

  case "$(uname -s)" in
    Darwin)
      os_name="darwin"
      ;;
    Linux)
      os_name="linux"
      ;;
    *)
      echo "ERROR: unsupported OS: $(uname -s). Supported: macOS, Linux." >&2
      return 1
      ;;
  esac

  case "$(uname -m)" in
    arm64|aarch64)
      arch_name="arm64"
      ;;
    x86_64|amd64)
      arch_name="amd64"
      ;;
    *)
      echo "ERROR: unsupported arch: $(uname -m). Supported: arm64, amd64." >&2
      return 1
      ;;
  esac

  REPLY="$os_name-$arch_name"
}

install_binary() {
  local platform="$1"
  local bin_dir="$INSTALL_DIR/bin"
  local bin_path="$bin_dir/kimi-webbridge"
  local tmp_bin
  local binary_url

  binary_url="$BASE_URL/$VERSION/releases/kimi-webbridge-$platform"
  log "Downloading Kimi WebBridge from $binary_url"
  mkdir -p "$bin_dir"
  tmp_bin="$(mktemp "${TMPDIR:-/tmp}/kimi-webbridge.XXXXXX")"
  if ! curl -fsSL --retry 3 --connect-timeout 10 -o "$tmp_bin" "$binary_url"; then
    rm -f "$tmp_bin"
    echo "ERROR: failed to download Kimi WebBridge binary" >&2
    return 1
  fi
  mv "$tmp_bin" "$bin_path"
  chmod +x "$bin_path"
  log "Installed $bin_path"
  REPLY="$bin_path"
}

start_daemon() {
  local bin_path="$1"

  if (( NO_START )); then
    log "Skipping daemon start (--no-start)"
    return 0
  fi

  log "Starting Kimi WebBridge daemon"
  "$bin_path" start
  log "Kimi WebBridge daemon started"
}

install_skill() {
  local bin_path="$1"

  if (( NO_SKILL )); then
    log "Skipping skill installation (--no-skill)"
    return 0
  fi

  log "Installing Kimi WebBridge agent skills"
  local install_status=0
  backup_system_skills
  "$bin_path" install-skill -y || install_status=$?
  restore_system_skills
  normalize_installed_skill
  if (( install_status != 0 )); then
    return "$install_status"
  fi
  log "Kimi WebBridge agent skills installed"
}

backup_system_skills() {
  local skills_dir="$HOME/.codex/skills"

  SYSTEM_SKILLS_BACKUP=""
  if [[ ! -d "$skills_dir/.system" ]]; then
    return 0
  fi

  SYSTEM_SKILLS_BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/kimi-webbridge-system-skills.XXXXXX")"
  tar -C "$skills_dir" -cf "$SYSTEM_SKILLS_BACKUP/system.tar" .system
}

restore_system_skills() {
  local skills_dir="$HOME/.codex/skills"

  if [[ -z "$SYSTEM_SKILLS_BACKUP" || ! -f "$SYSTEM_SKILLS_BACKUP/system.tar" ]]; then
    return 0
  fi

  rm -rf "$skills_dir/.system"
  tar -C "$skills_dir" -xf "$SYSTEM_SKILLS_BACKUP/system.tar"
  rm -rf "$SYSTEM_SKILLS_BACKUP"
  SYSTEM_SKILLS_BACKUP=""
}

normalize_installed_skill() {
  local skill_file="$HOME/.codex/skills/kimi-webbridge/SKILL.md"

  if [[ ! -f "$skill_file" ]]; then
    return 0
  fi

  perl -0pi -e 's/Daemon, extension, and this skill share a 1:1 version string\. Read both via:/Read daemon and extension versions via:/' "$skill_file"
  perl -0pi -e 's/the user'\''s extension is older than this skill/the user'\''s extension is older than the daemon or skill/' "$skill_file"
}

main() {
  local platform
  local bin_path

  parse_args "$@"
  require_command curl
  require_command mktemp
  require_command uname

  detect_platform
  platform="$REPLY"
  log "Platform: $platform"
  log "Version: $VERSION"

  install_binary "$platform"
  bin_path="$REPLY"
  start_daemon "$bin_path"
  install_skill "$bin_path"

  echo "Kimi WebBridge setup complete. Check status with: $bin_path status"
}

main "$@"
