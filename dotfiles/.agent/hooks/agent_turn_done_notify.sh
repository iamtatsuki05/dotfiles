#!/usr/bin/env bash

set -euo pipefail

if [[ "${DOTFILES_AGENT_NOTIFY:-1}" == "0" || "${DOTFILES_AGENT_NOTIFY_DISABLED:-}" == "1" ]]; then
  exit 0
fi

sound_file="${DOTFILES_AGENT_NOTIFY_SOUND:-/System/Library/Sounds/Ping.aiff}"

if command -v afplay >/dev/null 2>&1 && [[ -f "$sound_file" ]]; then
  afplay "$sound_file" >/dev/null 2>&1 || true
  exit 0
fi

if command -v osascript >/dev/null 2>&1; then
  osascript -e 'beep 1' >/dev/null 2>&1 || true
fi

exit 0
