#!/usr/bin/env zsh

set -euo pipefail

DEFAULT_LABEL="io.github.iamtatsuki05.agent-job-scheduler"
DEFAULT_OUTPUT="${HOME}/Library/LaunchAgents/${DEFAULT_LABEL}.plist"

label="$DEFAULT_LABEL"
output_path="$DEFAULT_OUTPUT"
unload_agent=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      label="$2"
      shift 2
      ;;
    --output)
      output_path="$2"
      shift 2
      ;;
    --no-unload)
      unload_agent=0
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$unload_agent" -eq 1 ]]; then
  launchctl bootout "gui/${UID}/${label}" >/dev/null 2>&1 || true
fi

rm -f "$output_path"
echo "removed ${label}: ${output_path}"
