#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly SCRIPT="$REPO_ROOT/scripts/agent_skill_upstreams.py"
readonly MANIFEST="$REPO_ROOT/dotfiles/.agent/skills/upstreams.json"
readonly DEFAULT_REVIEW_PROMPT="$REPO_ROOT/dotfiles/.agent/skills/review-prompts/skill-upstream-security.md"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains_text() {
  local text="$1"
  local expected="$2"

  [[ "$text" == *"$expected"* ]] || fail "expected output to contain: $expected"
}

assert_not_contains_text() {
  local text="$1"
  local unexpected="$2"

  [[ "$text" != *"$unexpected"* ]] || fail "expected output not to contain: $unexpected"
}

assert_file() {
  local file_path="$1"
  [[ -f "$file_path" ]] || fail "expected file: $file_path"
}

assert_executable() {
  local file_path="$1"
  [[ -x "$file_path" ]] || fail "expected executable: $file_path"
}

test_manifest_and_cli_exist() {
  assert_file "$MANIFEST"
  assert_file "$DEFAULT_REVIEW_PROMPT"
  assert_executable "$SCRIPT"
}

test_check_validates_registered_upstreams() {
  local output
  output="$(python3 "$SCRIPT" check)"

  assert_contains_text "$output" "registered upstream skills: 2"
  assert_contains_text "$output" "superpowers"
  assert_contains_text "$output" "empirical-prompt-tuning"
}

test_updates_accepts_fixture_ls_remote_output() {
  local output
  output="$(
    python3 "$SCRIPT" updates \
      --id empirical-prompt-tuning \
      --ls-remote-output "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa	refs/heads/main"
  )"

  assert_contains_text "$output" "empirical-prompt-tuning"
  assert_contains_text "$output" "update available"
  assert_contains_text "$output" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}

test_security_prompt_accepts_commit_alias() {
  local output
  output="$(
    python3 "$SCRIPT" security-prompt \
      --id empirical-prompt-tuning \
      --commit cccccccccccccccccccccccccccccccccccccccc
  )"

  assert_contains_text "$output" "empirical-prompt-tuning"
  assert_contains_text "$output" "レビュー担当 Agent: codex"
  assert_contains_text "$output" "candidate_commit: cccccccccccccccccccccccccccccccccccccccc"
}

test_security_prompt_accepts_registered_review_agent() {
  local output
  output="$(
    python3 "$SCRIPT" security-prompt \
      --id superpowers \
      --review-agent openclaw \
      --commit cccccccccccccccccccccccccccccccccccccccc
  )"

  assert_contains_text "$output" "レビュー担当 Agent: openclaw"
  assert_contains_text "$output" "Skill ID: superpowers"
}

test_security_prompt_rejects_unknown_review_agent() {
  local output
  set +e
  output="$(
    python3 "$SCRIPT" security-prompt \
      --id superpowers \
      --review-agent unknown-agent \
      --commit cccccccccccccccccccccccccccccccccccccccc 2>&1
  )"
  local exit_status=$?
  set -e

  [[ "$exit_status" -ne 0 ]] || fail "expected unknown review agent to fail"
  assert_contains_text "$output" "review agent must be one of"
}

test_security_prompt_accepts_custom_review_prompt() {
  local prompt_file
  local output

  prompt_file="$(mktemp)"
  cat > "$prompt_file" <<'EOF'
CUSTOM REVIEW PROMPT
レビュー担当 Agent: ${review_agent}
Skill ID: ${skill_id}
candidate_commit: ${candidate_commit}
Mappings:
${mappings}
EOF

  output="$(
    python3 "$SCRIPT" security-prompt \
      --id empirical-prompt-tuning \
      --review-prompt "$prompt_file" \
      --review-agent claude-code \
      --commit cccccccccccccccccccccccccccccccccccccccc
  )"

  assert_contains_text "$output" "CUSTOM REVIEW PROMPT"
  assert_contains_text "$output" "レビュー担当 Agent: claude-code"
  assert_contains_text "$output" "Skill ID: empirical-prompt-tuning"

  rm -f "$prompt_file"
}

test_security_prompt_all_generates_prompts_for_registered_skills() {
  local output
  output="$(
    python3 "$SCRIPT" security-prompt \
      --all \
      --latest-commit superpowers=dddddddddddddddddddddddddddddddddddddddd \
      --latest-commit empirical-prompt-tuning=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
  )"

  assert_contains_text "$output" "Skill ID: superpowers"
  assert_contains_text "$output" "candidate_commit: dddddddddddddddddddddddddddddddddddddddd"
  assert_contains_text "$output" "Skill ID: empirical-prompt-tuning"
  assert_contains_text "$output" "candidate_commit: eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
}

test_apply_update_all_latest_dry_run_requires_review_dir_and_plans_each_skill() {
  local report_dir
  local output

  report_dir="$(mktemp -d)"
  print -r -- "reviewed superpowers" > "$report_dir/superpowers.md"
  print -r -- "reviewed empirical-prompt-tuning" > "$report_dir/empirical-prompt-tuning.md"

  output="$(
    python3 "$SCRIPT" apply-update \
      --all \
      --latest \
      --review-report-dir "$report_dir" \
      --security-reviewed \
      --dry-run \
      --latest-commit superpowers=ffffffffffffffffffffffffffffffffffffffff \
      --latest-commit empirical-prompt-tuning=1111111111111111111111111111111111111111
  )"

  assert_contains_text "$output" "superpowers: plan update"
  assert_contains_text "$output" "candidate=ffffffffffffffffffffffffffffffffffffffff"
  assert_contains_text "$output" "empirical-prompt-tuning: plan update"
  assert_contains_text "$output" "candidate=1111111111111111111111111111111111111111"
  assert_not_contains_text "$output" "manifest updated"

  rm -rf "$report_dir"
}

test_apply_update_accepts_specific_commit() {
  local review_report
  local output

  review_report="$(mktemp)"
  print -r -- "reviewed empirical-prompt-tuning" > "$review_report"

  output="$(
    python3 "$SCRIPT" apply-update \
      --id empirical-prompt-tuning \
      --commit 2222222222222222222222222222222222222222 \
      --review-agent openclaw \
      --review-report "$review_report" \
      --security-reviewed \
      --dry-run
  )"

  assert_contains_text "$output" "empirical-prompt-tuning: plan update"
  assert_contains_text "$output" "candidate=2222222222222222222222222222222222222222"
  assert_contains_text "$output" "review_agent=openclaw"
  assert_not_contains_text "$output" "manifest updated"

  rm -f "$review_report"
}

test_security_prompt_contains_required_review_points() {
  local output
  output="$(
    python3 "$SCRIPT" security-prompt \
      --id superpowers \
      --candidate-commit bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  )"

  assert_contains_text "$output" "superpowers"
  assert_contains_text "$output" "pinned_commit"
  assert_contains_text "$output" "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  assert_contains_text "$output" "prompt injection"
  assert_contains_text "$output" "secret"
  assert_contains_text "$output" "破壊的コマンド"
  assert_contains_text "$output" "update recommendation"
}

test_update_defaults_to_all_latest_and_runs_agent_review_before_dry_run_apply() {
  local output
  local review_command

  review_command='mkdir -p "$(dirname "$AGENT_SKILL_REVIEW_REPORT")"; cat > "$AGENT_SKILL_REVIEW_REPORT" <<EOF
- review agent: codex
- security findings: None.
- compatibility findings: None.
- required local changes: None.
- update recommendation: approve
EOF'

  output="$(
    python3 "$SCRIPT" update \
      --dry-run \
      --review-command "$review_command" \
      --latest-commit superpowers=3333333333333333333333333333333333333333 \
      --latest-commit empirical-prompt-tuning=4444444444444444444444444444444444444444
  )"

  assert_contains_text "$output" "superpowers: review approved"
  assert_contains_text "$output" "empirical-prompt-tuning: review approved"
  assert_contains_text "$output" "superpowers: plan update"
  assert_contains_text "$output" "candidate=3333333333333333333333333333333333333333"
  assert_contains_text "$output" "empirical-prompt-tuning: plan update"
  assert_contains_text "$output" "candidate=4444444444444444444444444444444444444444"
  assert_not_contains_text "$output" "manifest updated"
}

test_update_reviews_all_skills_in_parallel() {
  local output
  local review_command
  local started_at
  local ended_at
  local elapsed

  review_command='sleep 1; mkdir -p "$(dirname "$AGENT_SKILL_REVIEW_REPORT")"; cat > "$AGENT_SKILL_REVIEW_REPORT" <<EOF
- review agent: codex
- security findings: None.
- compatibility findings: None.
- required local changes: None.
- update recommendation: approve
EOF'

  started_at="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  output="$(
    python3 "$SCRIPT" update \
      --dry-run \
      --review-command "$review_command" \
      --latest-commit superpowers=3333333333333333333333333333333333333333 \
      --latest-commit empirical-prompt-tuning=4444444444444444444444444444444444444444
  )"
  ended_at="$(python3 - <<'PY'
import time
print(time.monotonic())
PY
)"
  elapsed="$(python3 - "$started_at" "$ended_at" <<'PY'
import sys
print(float(sys.argv[2]) - float(sys.argv[1]))
PY
)"

  python3 - "$elapsed" <<'PY' || fail "expected parallel review execution, elapsed=${elapsed}s"
import sys
elapsed = float(sys.argv[1])
raise SystemExit(0 if elapsed < 1.8 else 1)
PY
  assert_contains_text "$output" "superpowers: review approved"
  assert_contains_text "$output" "empirical-prompt-tuning: review approved"
}

test_update_blocks_when_agent_review_does_not_approve() {
  local output
  local review_command

  review_command='mkdir -p "$(dirname "$AGENT_SKILL_REVIEW_REPORT")"; cat > "$AGENT_SKILL_REVIEW_REPORT" <<EOF
- review agent: codex
- security findings: High: risky instruction.
- compatibility findings: None.
- required local changes: Remove risky instruction.
- update recommendation: reject
EOF'

  set +e
  output="$(
    python3 "$SCRIPT" update \
      --dry-run \
      --review-command "$review_command" \
      --latest-commit superpowers=3333333333333333333333333333333333333333 \
      --latest-commit empirical-prompt-tuning=4444444444444444444444444444444444444444 2>&1
  )"
  local exit_status=$?
  set -e

  [[ "$exit_status" -ne 0 ]] || fail "expected rejected review to fail"
  assert_contains_text "$output" "review did not approve"
  assert_not_contains_text "$output" "plan update"
}

test_update_accepts_approve_with_changes_when_no_blocking_findings() {
  local output
  local review_command

  review_command='mkdir -p "$(dirname "$AGENT_SKILL_REVIEW_REPORT")"; cat > "$AGENT_SKILL_REVIEW_REPORT" <<EOF
- review agent: codex
- security findings: Critical: none. High: none. Medium: none. Low: none.
- compatibility findings: Follow-up eval coverage is recommended.
- required local changes: Update metadata and consider eval coverage.
- update recommendation: approve with changes.
EOF'

  output="$(
    python3 "$SCRIPT" update \
      --id empirical-prompt-tuning \
      --commit 6666666666666666666666666666666666666666 \
      --dry-run \
      --review-command "$review_command"
  )"

  assert_contains_text "$output" "empirical-prompt-tuning: review approved"
  assert_contains_text "$output" "empirical-prompt-tuning: plan update"
  assert_contains_text "$output" "candidate=6666666666666666666666666666666666666666"
}

test_update_can_limit_to_one_skill_with_specific_commit() {
  local output
  local review_command

  review_command='mkdir -p "$(dirname "$AGENT_SKILL_REVIEW_REPORT")"; cat > "$AGENT_SKILL_REVIEW_REPORT" <<EOF
- review agent: codex
- security findings: None.
- compatibility findings: None.
- required local changes: None.
- update recommendation: approve
EOF'

  output="$(
    python3 "$SCRIPT" update \
      --id empirical-prompt-tuning \
      --commit 5555555555555555555555555555555555555555 \
      --dry-run \
      --review-command "$review_command"
  )"

  assert_contains_text "$output" "empirical-prompt-tuning: review approved"
  assert_contains_text "$output" "empirical-prompt-tuning: plan update"
  assert_contains_text "$output" "candidate=5555555555555555555555555555555555555555"
  assert_not_contains_text "$output" "superpowers: plan update"
}

test_mise_has_agent_skill_update_task() {
  local mise_config="$REPO_ROOT/config/mise/config.toml"

  assert_file "$mise_config"
  assert_contains_text "$(cat "$mise_config")" "[tasks.agent-skill-update]"
  assert_contains_text "$(cat "$mise_config")" "python3 scripts/agent_skill_upstreams.py update"
}

main() {
  test_manifest_and_cli_exist
  test_check_validates_registered_upstreams
  test_updates_accepts_fixture_ls_remote_output
  test_security_prompt_accepts_commit_alias
  test_security_prompt_accepts_registered_review_agent
  test_security_prompt_rejects_unknown_review_agent
  test_security_prompt_accepts_custom_review_prompt
  test_security_prompt_all_generates_prompts_for_registered_skills
  test_apply_update_all_latest_dry_run_requires_review_dir_and_plans_each_skill
  test_apply_update_accepts_specific_commit
  test_security_prompt_contains_required_review_points
  test_update_defaults_to_all_latest_and_runs_agent_review_before_dry_run_apply
  test_update_reviews_all_skills_in_parallel
  test_update_blocks_when_agent_review_does_not_approve
  test_update_accepts_approve_with_changes_when_no_blocking_findings
  test_update_can_limit_to_one_skill_with_specific_commit
  test_mise_has_agent_skill_update_task
}

main "$@"
