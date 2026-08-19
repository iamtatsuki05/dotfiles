#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly ANALYZER="$REPO_ROOT/scripts/analyze_agent_delegation.py"

source "$TEST_DIR/lib/assertions.sh"

write_jsonl() {
  local path="$1"
  shift

  print -rl -- "$@" > "$path"
}

test_reports_dispatch_batches_and_overlap_without_content() {
  local sessions_root
  local output
  make_temp_dir
  sessions_root="$REPLY"
  mkdir -p "$sessions_root/2026/08/19"

  write_jsonl "$sessions_root/2026/08/19/root.jsonl" \
    '{"timestamp":"2026-08-19T00:00:00Z","type":"session_meta","payload":{"id":"root-1"}}' \
    '{"timestamp":"2026-08-19T00:00:01Z","type":"response_item","payload":{"type":"function_call","name":"spawn_agent","arguments":"{\"message\":\"PRIVATE_PROMPT_MUST_NOT_LEAK\"}"}}' \
    '{"timestamp":"2026-08-19T00:00:02Z","type":"response_item","payload":{"type":"function_call","name": "spawn_agent","arguments":"{\"message\":\"second private prompt\"}"}}' \
    '{"timestamp":"2026-08-19T00:00:03Z","type":"response_item","payload":{"type":"function_call","name":"wait_agent","arguments":"{}"}}' \
    '{"timestamp":"2026-08-19T00:01:00Z","type":"response_item","payload":{"type":"function_call","name":"spawn_agent","arguments":"{\"message\":\"third private prompt\"}"}}' \
    '{"timestamp":"2026-08-19T00:01:01Z","type":"response_item","payload":{"type":"function_call","name":"wait_agent","arguments":"{}"}}'

  write_jsonl "$sessions_root/2026/08/19/child-1.jsonl" \
    '{"timestamp":"2026-08-19T00:00:01Z","type":"session_meta","payload":{"id":"child-1","timestamp":"2026-08-19T00:00:01Z","thread_source":"subagent","source":{"subagent":{"thread_spawn":{"parent_thread_id":"root-1","depth":1}}}}}' \
    '{"timestamp":"2026-08-19T00:00:01Z","type":"event_msg","payload":{"type":"task_started","turn_id":"historical","started_at":1787094000}}' \
    '{"timestamp":"2026-08-19T00:00:01.001Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"historical","started_at":1787094000,"completed_at":1787094001,"duration_ms":1000}}' \
    '{"timestamp":"2026-08-19T00:00:01Z","type":"event_msg","payload":{"type":"task_started","turn_id":"child-1-a","started_at":1787097601}}' \
    '{"timestamp":"2026-08-19T00:00:11Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"child-1-a","started_at":1787097601,"completed_at":1787097611,"duration_ms":10000,"message":"PRIVATE_RESULT_MUST_NOT_LEAK"}}' \
    '{"timestamp":"2026-08-19T00:02:00Z","type":"event_msg","payload":{"type":"task_started","turn_id":"child-1-b","started_at":1787097720}}' \
    '{"timestamp":"2026-08-19T00:02:03Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"child-1-b","started_at":1787097720,"completed_at":1787097723,"duration_ms":3000}}'

  write_jsonl "$sessions_root/2026/08/19/child-2.jsonl" \
    '{"timestamp":"2026-08-19T00:00:02Z","type":"session_meta","payload":{"id":"child-2","timestamp":"2026-08-19T00:00:02Z","thread_source":"subagent","source":{"subagent":{"thread_spawn":{"parent_thread_id":"root-1","depth":1}}}}}' \
    '{"timestamp":"2026-08-19T00:00:02Z","type":"event_msg","payload":{"type":"task_started","turn_id":"child-2-a","started_at":1787097602}}' \
    '{"timestamp":"2026-08-19T00:00:07Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"child-2-a","completed_at":1787097607,"duration_ms":5000}}'

  write_jsonl "$sessions_root/2026/08/19/child-3.jsonl" \
    '{"timestamp":"2026-08-19T00:01:00Z","type":"session_meta","payload":{"id":"child-3","timestamp":"2026-08-19T00:01:00Z","thread_source":"subagent","source":{"subagent":{"thread_spawn":{"parent_thread_id":"root-1","depth":1}}}}}' \
    '{"timestamp":"2026-08-19T00:01:00Z","type":"event_msg","payload":{"type": "task_started","turn_id":"child-3-a","started_at":1787097660}}' \
    '{"timestamp":"2026-08-19T00:01:04Z","type":"event_msg","payload":{"type": "task_complete","turn_id":"child-3-a","started_at":1787097660,"completed_at":1787097664,"duration_ms":4000}}'

  output="$(python3 "$ANALYZER" --sessions-root "$sessions_root")"

  print -r -- "$output" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
dispatch = payload["dispatch"]
runs = payload["task_runs"]

assert payload["schema_version"] == 1
assert payload["privacy"] == {
    "emits_prompt_content": False,
    "emits_response_content": False,
    "emits_tool_arguments": False,
    "emits_tool_outputs": False,
}
assert dispatch["spawn_calls"] == 3
assert dispatch["wait_calls"] == 2
assert dispatch["batches"] == 2
assert dispatch["single_spawn_batches"] == 1
assert dispatch["multi_spawn_batches"] == 1
assert dispatch["spawns_in_multi_batches"] == 2
assert dispatch["batch_size"] == {"n": 2, "median": 1.5, "p90": 2.0, "max": 2.0}
assert runs["count"] == 4
assert runs["parent_threads"] == 1
assert runs["incomplete"] == 0
assert runs["duration_seconds"] == {"n": 4, "median": 4.5, "p90": 10.0, "max": 10.0}
assert runs["peak_concurrency_per_parent"] == {"n": 1, "median": 2.0, "p90": 2.0, "max": 2.0}
assert runs["overlap_waves"]["multi_agent_waves"] == 1
assert runs["overlap_waves"]["sequential_equivalent_seconds"] == 15.0
assert runs["overlap_waves"]["wall_clock_span_seconds"] == 10.0
assert runs["overlap_waves"]["observed_overlap_seconds"] == 5.0
'

  [[ "$output" != *"PRIVATE_PROMPT_MUST_NOT_LEAK"* ]] || fail "analyzer leaked prompt content"
  [[ "$output" != *"PRIVATE_RESULT_MUST_NOT_LEAK"* ]] || fail "analyzer leaked result content"

  rm -rf "$sessions_root"
}

test_rejects_missing_sessions_root() {
  local missing_root="${TMPDIR:-/tmp}/dotfiles-agent-delegation-missing-$$"

  if python3 "$ANALYZER" --sessions-root "$missing_root" >/dev/null 2>&1; then
    fail "expected missing sessions root to fail"
  fi
}

main() {
  test_reports_dispatch_batches_and_overlap_without_content
  test_rejects_missing_sessions_root
  echo "agent delegation analysis tests passed"
}

main "$@"
