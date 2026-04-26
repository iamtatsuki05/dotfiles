#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

exec zsh "$REPO_ROOT/scripts/setup_agent_files.sh" --repo-root "$REPO_ROOT" "$@"
