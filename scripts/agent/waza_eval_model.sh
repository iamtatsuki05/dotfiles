#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly DEFAULT_AGENT="codex"

usage() {
  cat <<EOF
Usage:
  zsh scripts/waza_eval_model.sh [AGENT] [options]
  zsh scripts/waza_eval_model.sh --agent AGENT [options]
  zsh scripts/waza_eval_model.sh --model AGENT [options]

Agents:
  codex, claude, antigravity, copilot, devin, cursor, opencode, hermes, openclaw, all

Aliases:
  claude-code -> claude
  agy, antigravity-cli -> antigravity
  cursor-agent -> cursor
  hermes-agent -> hermes

Options:
  --agent AGENT  Select the CLI agent to run. Default: ${DEFAULT_AGENT}.
  --model AGENT  Alias for --agent, kept for the Waza model-eval command shape.
  --allow        Run CLI-backed evals. These require CLI credentials and may use paid quota.
  --dry-run      Print the suites and tasks that would run without invoking an AI CLI.
  --suite PATH   Run one model-backed Waza eval suite. May be specified multiple times.
  --output-dir PATH
                 Store captured prompts, stdout, stderr, and grading summaries.
  --keep-workspace
                 Keep each temporary fixture workspace for debugging.
  -h, --help     Show this help.
EOF
}

main() {
  local agent="$DEFAULT_AGENT"
  local agent_set=0
  local allow=0
  local dry_run=0
  local -a forwarded_args
  forwarded_args=()

  while (($#)); do
    case "$1" in
      --agent|--model)
        shift
        if ((! $#)); then
          echo "ERROR: --agent/--model requires an agent such as codex, claude, or copilot" >&2
          usage >&2
          return 1
        fi
        agent="$1"
        agent_set=1
        ;;
      -h|--help)
        usage
        return 0
        ;;
      --allow)
        allow=1
        forwarded_args+=("$1")
        ;;
      --dry-run)
        dry_run=1
        forwarded_args+=("$1")
        ;;
      --keep-workspace)
        forwarded_args+=("$1")
        ;;
      --suite|--output-dir)
        forwarded_args+=("$1")
        shift
        if ((! $#)); then
          echo "ERROR: ${forwarded_args[-1]} requires a value" >&2
          usage >&2
          return 1
        fi
        forwarded_args+=("$1")
        ;;
      --*)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
      *)
        if (( agent_set )); then
          echo "ERROR: agent was specified more than once: $1" >&2
          usage >&2
          return 1
        fi
        agent="$1"
        agent_set=1
        ;;
    esac
    shift
  done

  if (( ! dry_run && ! allow )); then
    cat >&2 <<EOF
Waza model evals require explicit --allow because local AI CLI credentials may use paid quota and can modify temporary workspaces.
Re-run with:
  zsh scripts/waza_eval_model.sh --allow
EOF
    return 2
  fi

  zsh "$SCRIPT_DIR/waza_eval_cli_agent.sh" "$agent" "${forwarded_args[@]}"
}

main "$@"
