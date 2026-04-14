#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly CRON_TEMPLATE_FILE="$REPO_ROOT/config/cron/crontab"
readonly CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
readonly BEGIN_MARKER="# >>> dotfiles managed cron >>>"
readonly END_MARKER="# <<< dotfiles managed cron <<<"

log() {
  echo "$*"
}

crontab_exists() {
  command -v "$CRONTAB_BIN" >/dev/null 2>&1
}

read_current_crontab() {
  if "$CRONTAB_BIN" -l 2>/dev/null; then
    return 0
  fi
}

strip_managed_block() {
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    skip != 1 { print }
  '
}

template_has_enabled_jobs() {
  awk '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/ { next }
    { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$CRON_TEMPLATE_FILE"
}

install_managed_crontab() {
  local current_file
  local stripped_file
  local merged_file
  current_file="$(mktemp)"
  stripped_file="$(mktemp)"
  merged_file="$(mktemp)"

  {
    read_current_crontab >"$current_file" || true
    strip_managed_block <"$current_file" >"$stripped_file"

    {
      cat "$stripped_file"

      if [ -s "$stripped_file" ]; then
        echo
      fi

      echo "$BEGIN_MARKER"
      cat "$CRON_TEMPLATE_FILE"
      echo "$END_MARKER"
    } >"$merged_file"

    "$CRONTAB_BIN" "$merged_file"
    log "Installed dotfiles managed cron block"
  } always {
    rm -f "$current_file" "$stripped_file" "$merged_file"
  }
}

remove_managed_crontab() {
  local current_file
  local stripped_file
  current_file="$(mktemp)"
  stripped_file="$(mktemp)"

  {
    read_current_crontab >"$current_file" || true
    strip_managed_block <"$current_file" >"$stripped_file"
    "$CRONTAB_BIN" "$stripped_file"
    log "Removed dotfiles managed cron block because no enabled jobs were found"
  } always {
    rm -f "$current_file" "$stripped_file"
  }
}

main() {
  if ! crontab_exists; then
    log "Skipped: crontab command not found"
    return 0
  fi

  if [ ! -f "$CRON_TEMPLATE_FILE" ]; then
    log "Skipped: $CRON_TEMPLATE_FILE not found"
    return 0
  fi

  if template_has_enabled_jobs; then
    install_managed_crontab
  else
    remove_managed_crontab
  fi
}

main "$@"
