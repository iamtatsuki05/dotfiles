#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h:h}"

main() {
  cd "$REPO_ROOT"

  local -a eval_files
  eval_files=(dotfiles/.agent/evals/*/eval.yaml(N))

  if (( ${#eval_files[@]} == 0 )); then
    echo "ERROR: no Waza eval suites found under dotfiles/.agent/evals" >&2
    return 1
  fi

  local eval_file
  local eval_dir
  local context_dir
  local -a waza_args
  for eval_file in "${eval_files[@]}"; do
    eval_dir="${eval_file:h}"
    context_dir="$eval_dir/fixtures"
    waza_args=(run "$eval_file" --output-dir .waza-results -v)
    if [[ -d "$context_dir" ]]; then
      waza_args+=(--context-dir "$context_dir")
    fi
    echo "===> Running Waza eval: $eval_file"
    WAZA_NO_UPDATE_CHECK=1 nix run path:.#waza -- "${waza_args[@]}"
  done
}

main "$@"
