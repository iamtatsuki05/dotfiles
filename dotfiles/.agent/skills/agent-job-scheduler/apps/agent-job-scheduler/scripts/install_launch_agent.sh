#!/usr/bin/env zsh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_LABEL="io.github.iamtatsuki05.agent-job-scheduler"
DEFAULT_RUNTIME_ROOT="${HOME}/.agent/agent-job-scheduler"
DEFAULT_INTERVAL_SECONDS="60"
DEFAULT_OUTPUT="${HOME}/Library/LaunchAgents/${DEFAULT_LABEL}.plist"

label="$DEFAULT_LABEL"
runtime_root="$DEFAULT_RUNTIME_ROOT"
interval_seconds="$DEFAULT_INTERVAL_SECONDS"
output_path="$DEFAULT_OUTPUT"
load_agent=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      label="$2"
      shift 2
      ;;
    --runtime-root)
      runtime_root="$2"
      shift 2
      ;;
    --interval-seconds)
      interval_seconds="$2"
      shift 2
      ;;
    --output)
      output_path="$2"
      shift 2
      ;;
    --no-load)
      load_agent=0
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$(dirname "$output_path")"
mkdir -p "${runtime_root}/logs"

"$PROJECT_DIR/bin/agent-job-scheduler" print-launchd-plist \
  --runtime-root "$runtime_root" \
  --label "$label" \
  --interval-seconds "$interval_seconds" \
  > "$output_path"

if [[ "$load_agent" -eq 1 ]]; then
  launchctl bootout "gui/${UID}/${label}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${UID}" "$output_path"
  launchctl kickstart -k "gui/${UID}/${label}"
  echo "installed and loaded ${label}: ${output_path}"
else
  echo "rendered ${label}: ${output_path}"
fi
