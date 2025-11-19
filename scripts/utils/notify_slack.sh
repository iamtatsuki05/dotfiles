#!/bin/bash

set -euo pipefail

# =============================================================================
# Execute command with Slack notifications for start and completion
# Usage: notify_slack.sh "command to execute"
# =============================================================================

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
readonly WEBHOOK_URL=""
readonly SLACK_USERNAME="ハチワレちゃん"
readonly LOG_DIR="${NOTIFY_SLACK_LOG_DIR:-$HOME/.notify_slack/logs}"

# Slack notification colors
readonly COLOR_INFO="#003399"
readonly COLOR_SUCCESS="#2eb886"
readonly COLOR_ERROR="#FF0000"
readonly COLOR_GRAY="#808080"

# Monitoring configuration
readonly POLL_INTERVAL_SEC=5
readonly LOG_TAIL_LINES=5

# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
if [[ $# -ne 1 ]]; then
  echo "ERROR: Exactly one command argument is required" >&2
  echo "Usage: $0 \"command to execute\"" >&2
  exit 1
fi

readonly COMMAND="$1"
readonly COMMAND_LABEL="${COMMAND##*/}"
readonly COMMAND_SAFE="$(echo "$COMMAND_LABEL" | tr -c 'A-Za-z0-9._-' '_')"

# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------
# Ensure log directory exists with absolute path
LOG_DIR_ABS="$(cd "$(dirname "$LOG_DIR")" 2>/dev/null && pwd)/$(basename "$LOG_DIR")" || LOG_DIR_ABS="$HOME/.notify_slack/logs"
readonly LOG_DIR_ABS
mkdir -p "$LOG_DIR_ABS"

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
send_slack_notification() {
  local payload="$1"
  curl -s -X POST --data-urlencode "$payload" "$WEBHOOK_URL" >/dev/null
}

notify_start() {
  local pid="$1"
  local log_file="$2"
  local text="\`\$ $COMMAND\`"
  local log_path_message="\`$log_file\`"

  local payload
  payload="payload={\"username\": \"$SLACK_USERNAME\", \"text\": \"追跡開始！ (PID： \`$pid\` )\", \"attachments\": [{\"fallback\": \"実行コマンド確認\",\"color\": \"$COLOR_INFO\",\"title\": \"実行コマンド\",\"text\": \"$text\"},{\"color\": \"$COLOR_GRAY\",\"title\": \"ログパス\",\"text\": \"$log_path_message\"}]}"

  send_slack_notification "$payload"
}

notify_completion() {
  local pid="$1"
  local log_file="$2"
  local exit_code="$3"

  local status_color
  local result_label

  if [[ $exit_code -eq 0 ]]; then
    status_color="$COLOR_SUCCESS"
    result_label="成功"
  else
    status_color="$COLOR_ERROR"
    result_label="失敗 (exit $exit_code)"
  fi

  local end_msg="${COMMAND_LABEL} が終了したってコト!? (PID： \`$pid\` / ${result_label})"
  local last_lines
  last_lines="$(tail -n "$LOG_TAIL_LINES" "$log_file")"
  local log_path_message="\`$log_file\`"

  local payload
  payload="payload={\"username\": \"$SLACK_USERNAME\", \"text\": \"${end_msg}\", \"attachments\": [{\"color\": \"$status_color\",\"title\": \"コンソールの最後の${LOG_TAIL_LINES}行\",\"text\": \"\`\`\`$last_lines\`\`\`\"},{\"color\": \"$COLOR_GRAY\",\"title\": \"フルログパス\",\"text\": \"$log_path_message\"}]}"

  send_slack_notification "$payload"
}

monitor_process() {
  local pid="$1"
  local log_file="$2"
  local exit_code_file="$3"

  # Wait for process completion
  while ps -p "$pid" >/dev/null 2>&1; do
    sleep "$POLL_INTERVAL_SEC"
  done

  # Allow exit code to be written
  sleep 1

  # Retrieve exit code
  local exit_code
  if [[ -f "$exit_code_file" ]]; then
    exit_code="$(cat "$exit_code_file")"
    rm -f "$exit_code_file"
  else
    exit_code=1
  fi

  notify_completion "$pid" "$log_file" "$exit_code"
}

# -----------------------------------------------------------------------------
# Main execution
# -----------------------------------------------------------------------------
main() {
  # Create temporary files
  local tmp_log_file
  local exit_code_file
  tmp_log_file="$(mktemp "$LOG_DIR_ABS/tmp.XXXXXX")"
  exit_code_file="$(mktemp "$LOG_DIR_ABS/exit_code.XXXXXX")"

  # Execute command in background
  (eval "$COMMAND" > "$tmp_log_file" 2>&1; echo $? > "$exit_code_file") &
  local pid=$!

  # Rename log file with PID
  local log_file="$LOG_DIR_ABS/${COMMAND_SAFE}_${pid}.log"
  mv -f "$tmp_log_file" "$log_file"

  # Log to stderr for user visibility
  echo "INFO: PID=$pid" >&2
  echo "INFO: LOG=$log_file" >&2
  echo "INFO: To kill process: kill $pid" >&2

  # Send start notification
  notify_start "$pid" "$log_file"

  # Monitor process in background
  monitor_process "$pid" "$log_file" "$exit_code_file" &
}

main "$@"
