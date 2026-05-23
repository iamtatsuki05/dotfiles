#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"

exec zsh "$SCRIPT_DIR/agent/waza_eval_model.sh" "$@"
