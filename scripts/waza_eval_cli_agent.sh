#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_ROOT="${SCRIPT_DIR:h}"
readonly DEFAULT_OUTPUT_DIR=".waza-results/cli-agents"

usage() {
  cat <<EOF
Usage:
  zsh scripts/waza_eval_cli_agent.sh codex|claude|gemini [--allow] [--dry-run] [--suite PATH]...

Options:
  --allow       Run CLI-backed evals. These require CLI credentials and may use paid quota.
  --dry-run     Print the suites and tasks that would run without invoking an AI CLI.
  --suite PATH  Run one model-backed Waza eval suite. May be specified multiple times.
                Defaults to dotfiles/.agent/evals/*/model.yaml.
  --output-dir PATH
                Store captured prompts, stdout, stderr, and grading summaries.
                Defaults to ${DEFAULT_OUTPUT_DIR}.
  --keep-workspace
                Keep each temporary fixture workspace for debugging.
  -h, --help    Show this help.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  return 1
}

repo_relative() {
  local path="$1"
  path="${path:A}"
  if [[ "$path" == "$REPO_ROOT/"* ]]; then
    print -- "${path#$REPO_ROOT/}"
  else
    print -- "$path"
  fi
}

suite_name() {
  local suite_file="$1"
  awk -F': *' '$1 == "name" { value=$2; gsub(/^"|"$/, "", value); print value; exit }' "$suite_file"
}

suite_skill() {
  local suite_file="$1"
  awk -F': *' '$1 == "skill" { value=$2; gsub(/^"|"$/, "", value); print value; exit }' "$suite_file"
}

task_id() {
  local task_file="$1"
  local id
  id="$(awk -F': *' '$1 == "id" { value=$2; gsub(/^"|"$/, "", value); print value; exit }' "$task_file")"
  if [[ -n "$id" ]]; then
    print -- "$id"
  else
    print -- "${task_file:t:r}"
  fi
}

task_refs_for_suite() {
  local suite_file="$1"
  awk '
    /^[[:space:]]*-[[:space:]]*["'\'']?tasks\// {
      line = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      gsub(/"/, "", line)
      gsub(/\047/, "", line)
      print line
    }
  ' "$suite_file"
}

regexes_for_suite_key() {
  local suite_file="$1"
  local key="$2"
  awk -v key="$key" '
    function indent_width(line) {
      match(line, /[^[:space:]]/)
      if (RSTART == 0) {
        return length(line)
      }
      return RSTART - 1
    }
    $0 ~ "^[[:space:]]*" key ":" {
      in_block = 1
      key_indent = indent_width($0)
      next
    }
    in_block && /^[[:space:]]*$/ {
      next
    }
    in_block && indent_width($0) <= key_indent {
      in_block = 0
      next
    }
    in_block && /^[[:space:]]*-[[:space:]]*/ {
      line = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", line)
      gsub(/^"/, "", line)
      gsub(/"$/, "", line)
      gsub(/^\047/, "", line)
      gsub(/\047$/, "", line)
      print line
      next
    }
  ' "$suite_file"
}

pattern_matches_file() {
  local pattern="$1"
  local file_path="$2"
  perl -0777 -e '
    my ($pattern, $file_path) = @ARGV;
    open my $fh, "<", $file_path or die "failed to read $file_path: $!";
    local $/;
    my $output = <$fh>;
    exit($output =~ /$pattern/ ? 0 : 1);
  ' "$pattern" "$file_path"
}

grade_output() {
  local suite_file="$1"
  local output_file="$2"
  local summary_file="$3"
  local failed=0
  local byte_count
  local regex
  local -a match_regexes
  local -a not_match_regexes

  byte_count="$(wc -c < "$output_file" | tr -d '[:space:]')"
  match_regexes=("${(@f)$(regexes_for_suite_key "$suite_file" "regex_match")}")
  not_match_regexes=("${(@f)$(regexes_for_suite_key "$suite_file" "regex_not_match")}")

  {
    echo "output_bytes: $byte_count"
    if (( byte_count > 80 )); then
      echo "PASS len(output) > 80"
    else
      echo "FAIL len(output) > 80"
      failed=1
    fi

    for regex in "${match_regexes[@]}"; do
      [[ -n "$regex" ]] || continue
      if pattern_matches_file "$regex" "$output_file"; then
        echo "PASS regex_match: $regex"
      else
        echo "FAIL regex_match: $regex"
        failed=1
      fi
    done

    for regex in "${not_match_regexes[@]}"; do
      [[ -n "$regex" ]] || continue
      if pattern_matches_file "$regex" "$output_file"; then
        echo "FAIL regex_not_match: $regex"
        failed=1
      else
        echo "PASS regex_not_match: $regex"
      fi
    done
  } > "$summary_file"

  return "$failed"
}

build_prompt() {
  local skill="$1"
  local eval_name="$2"
  local suite_file="$3"
  local task_file="$4"
  local workspace="$5"
  local prompt_file="$6"

  {
    echo "You are running a local CLI-agent evaluation for a Codex skill."
    echo "Work only inside the current temporary workspace. Do not modify the dotfiles repository or the user's home directory."
    echo
    echo "Skill: ${skill:-unknown}"
    echo "Eval suite: ${eval_name:-unknown}"
    echo "Suite file: $(repo_relative "$suite_file")"
    echo "Task file: $(repo_relative "$task_file")"
    echo
    echo "Task YAML:"
    sed 's/^/    /' "$task_file"
    echo
    echo "Available fixture files in this workspace:"
    (cd "$workspace" && find . -type f | sort | sed 's#^\./#    #')
    echo
    echo "Complete the task described by the YAML. Return the answer only; do not include unrelated setup commentary."
  } > "$prompt_file"
}

run_cli_agent() {
  local agent="$1"
  local workspace="$2"
  local prompt_file="$3"
  local stdout_file="$4"
  local stderr_file="$5"
  local prompt

  prompt="$(< "$prompt_file")"
  case "$agent" in
    codex)
      command -v codex >/dev/null || fail "codex CLI is not on PATH"
      codex exec -C "$workspace" "$prompt" >"$stdout_file" 2>"$stderr_file"
      ;;
    claude)
      command -v claude >/dev/null || fail "claude CLI is not on PATH"
      (cd "$workspace" && claude -p "$prompt") >"$stdout_file" 2>"$stderr_file"
      ;;
    gemini)
      command -v gemini >/dev/null || fail "gemini CLI is not on PATH"
      (cd "$workspace" && gemini -p "$prompt") >"$stdout_file" 2>"$stderr_file"
      ;;
    *)
      fail "unsupported agent: $agent"
      ;;
  esac
}

copy_fixtures() {
  local eval_dir="$1"
  local workspace="$2"
  local fixtures_dir="$eval_dir/fixtures"

  if [[ -d "$fixtures_dir" ]]; then
    cp -R "$fixtures_dir"/. "$workspace"/
  fi
}

run_task() {
  local agent="$1"
  local suite_file="$2"
  local task_ref="$3"
  local output_dir="$4"
  local keep_workspace="$5"
  local eval_dir="${suite_file:h}"
  local task_file="$eval_dir/$task_ref"
  local skill
  local eval_name
  local id
  local result_dir
  local workspace
  local prompt_file
  local stdout_file
  local stderr_file
  local summary_file
  local cli_status=0
  local grade_status=0

  [[ -f "$task_file" ]] || fail "task file not found: $(repo_relative "$task_file")"

  skill="$(suite_skill "$suite_file")"
  eval_name="$(suite_name "$suite_file")"
  id="$(task_id "$task_file")"
  result_dir="$output_dir/$agent/${eval_name:-${suite_file:h:t}}/$id"
  workspace="$(mktemp -d "${TMPDIR:-/tmp}/waza-cli-agent-${agent}-${id}.XXXXXX")"

  mkdir -p "$result_dir"
  prompt_file="$result_dir/prompt.txt"
  stdout_file="$result_dir/stdout.txt"
  stderr_file="$result_dir/stderr.txt"
  summary_file="$result_dir/summary.txt"

  copy_fixtures "$eval_dir" "$workspace"
  build_prompt "$skill" "$eval_name" "$suite_file" "$task_file" "$workspace" "$prompt_file"

  echo "===> Running $agent: $(repo_relative "$suite_file") $task_ref"
  run_cli_agent "$agent" "$workspace" "$prompt_file" "$stdout_file" "$stderr_file" || cli_status=$?
  if (( cli_status == 0 )); then
    grade_output "$suite_file" "$stdout_file" "$summary_file" || grade_status=$?
  else
    echo "CLI failed with status $cli_status" > "$summary_file"
  fi

  if (( ! keep_workspace )); then
    rm -rf "$workspace"
  else
    echo "$workspace" > "$result_dir/workspace.txt"
  fi

  if (( cli_status != 0 )); then
    return "$cli_status"
  fi
  return "$grade_status"
}

main() {
  if (($# == 0)); then
    usage >&2
    return 1
  fi

  local agent="$1"
  shift
  case "$agent" in
    codex|claude|gemini) ;;
    -h|--help)
      usage
      return 0
      ;;
    *)
      usage >&2
      return 1
      ;;
  esac

  local allow=0
  local dry_run=0
  local keep_workspace=0
  local output_dir="$DEFAULT_OUTPUT_DIR"
  local -a suites
  suites=()

  while (($#)); do
    case "$1" in
      --allow)
        allow=1
        ;;
      --dry-run)
        dry_run=1
        ;;
      --suite)
        shift
        (($#)) || fail "--suite requires a path"
        suites+=("$1")
        ;;
      --output-dir)
        shift
        (($#)) || fail "--output-dir requires a path"
        output_dir="$1"
        ;;
      --keep-workspace)
        keep_workspace=1
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        fail "unknown argument: $1"
        ;;
    esac
    shift
  done

  cd "$REPO_ROOT"

  if (( ${#suites[@]} == 0 )); then
    suites=(dotfiles/.agent/evals/*/model.yaml(N))
  fi
  if (( ${#suites[@]} == 0 )); then
    fail "no model-backed eval suites found under dotfiles/.agent/evals"
  fi

  if (( ! dry_run && ! allow )); then
    cat >&2 <<EOF
CLI agent evals require explicit --allow because Codex / Claude Code / Gemini CLI credentials may use paid quota and can modify their temporary workspaces.
Re-run with:
  zsh scripts/waza_eval_cli_agent.sh $agent --allow
EOF
    return 2
  fi

  local suite_file
  local task_ref
  local failed=0
  for suite_file in "${suites[@]}"; do
    [[ -f "$suite_file" ]] || fail "suite file not found: $suite_file"
    local -a task_refs
    task_refs=("${(@f)$(task_refs_for_suite "$suite_file")}")
    if (( ${#task_refs[@]} == 0 )); then
      fail "suite has no tasks: $(repo_relative "$suite_file")"
    fi

    for task_ref in "${task_refs[@]}"; do
      [[ -n "$task_ref" ]] || continue
      if (( dry_run )); then
        echo "DRY-RUN $agent $(repo_relative "$suite_file") $task_ref"
      elif ! run_task "$agent" "$suite_file" "$task_ref" "$output_dir" "$keep_workspace"; then
        failed=1
      fi
    done
  done

  return "$failed"
}

main "$@"
