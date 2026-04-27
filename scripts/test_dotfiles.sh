#!/usr/bin/zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"

if [[ -x /bin/zsh ]]; then
  zsh_bin="/bin/zsh"
elif [[ -x /usr/bin/zsh ]]; then
  zsh_bin="/usr/bin/zsh"
else
  zsh_bin="zsh"
fi

exec "$zsh_bin" "$REPO_ROOT/tests/run.sh" "$@"
