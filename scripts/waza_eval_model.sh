#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"

usage() {
  cat <<EOF
Usage:
  zsh scripts/waza_eval_model.sh [--allow]

Options:
  --allow   Run model-backed Waza evals. These require model credentials and may use paid quota.
  -h, --help
            Show this help.
EOF
}

main() {
  local allow=0
  while (($#)); do
    case "$1" in
      --allow)
        allow=1
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
    esac
    shift
  done

  if (( ! allow )); then
    cat >&2 <<'EOF'
Waza model-backed evals require model credentials and may use paid quota.
Re-run with:
  zsh scripts/waza_eval_model.sh --allow
EOF
    return 2
  fi

  cd "$REPO_ROOT"

  local -a eval_files
  eval_files=(dotfiles/.agent/evals/*/model.yaml(N))

  if (( ${#eval_files[@]} == 0 )); then
    echo "ERROR: no model-backed Waza eval suites found under dotfiles/.agent/evals" >&2
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
    echo "===> Running model-backed Waza eval: $eval_file"
    WAZA_NO_UPDATE_CHECK=1 nix run path:.#waza -- "${waza_args[@]}"
  done
}

main "$@"
