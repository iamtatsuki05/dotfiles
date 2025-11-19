#!/bin/bash

# Usage(run): `sh notify_slack.sh "bash start_gcp_instance.sh"`

set -eu

# ====================
# ユーザー設定
# ====================
URL=""  # Webhook URLを使用してください
USERNAME="ハチワレちゃん"
LOG_DIR="${NOTIFY_SLACK_LOG_DIR:-$HOME/.notify_slack/logs}"  # ログファイル出力先ディレクトリ

if [ "$#" -ne 1 ]; then
  echo "ERROR: A single command string is required." >&2
  exit 1
fi

COMMAND=$1
COMMAND_LABEL="${COMMAND##*/}"
COMMAND_SAFE=$(echo "$COMMAND_LABEL" | tr -c 'A-Za-z0-9._-' '_')

# ログディレクトリを絶対パスに変換
LOG_DIR=$(cd "$(dirname "$LOG_DIR")" 2>/dev/null && pwd)/$(basename "$LOG_DIR") || LOG_DIR="$HOME/.notify_slack/logs"
mkdir -p "$LOG_DIR"

ensure_log_notice() {
  echo "INFO: PID=$PID" >&2
  echo "INFO: LOG=$LOG_FILE" >&2
}

# コマンドをバックグラウンドで実行し、出力を一時ファイルに記録
# 一時ファイルに出力してからPIDベースのファイル名にリネーム
TMP_LOG_FILE=$(mktemp "$LOG_DIR/tmp.XXXXXX")
EXIT_CODE_FILE=$(mktemp "$LOG_DIR/exit_code.XXXXXX")
(eval "$COMMAND" > "$TMP_LOG_FILE" 2>&1; echo $? > "$EXIT_CODE_FILE") &
PID=$!
LOG_FILE="$LOG_DIR/${COMMAND_SAFE}_${PID}.log"
mv -f "$TMP_LOG_FILE" "$LOG_FILE"
ensure_log_notice

# 実行開始時のプロセス
TEXT="\`\$ $COMMAND\`"
LOG_PATH_MESSAGE="\`$LOG_FILE\`"

TITLE="実行コマンド"
BEGIN_DATA="payload={\"username\": \"$USERNAME\", \"text\": \"追跡開始！ (PID： \`$PID\` )\", \"attachments\": [{\"fallback\": \"実行コマンド確認\",\"color\": \"#003399\",\"title\": \"$TITLE\",\"text\": \"$TEXT\"},{\"color\": \"#808080\",\"title\": \"ログパス\",\"text\": \"$LOG_PATH_MESSAGE\"}]}"

curl -s -X POST --data-urlencode "$BEGIN_DATA" ${URL} >/dev/null

# プロセスの終了監視をバックグラウンドで実行
{
  # プロセスが終了するまで待機
  while ps -p "$PID" >/dev/null 2>&1; do
    sleep 5
  done

  # 終了コードファイルが作成されるまで少し待機
  sleep 1

  # 終了コードを取得
  if [ -f "$EXIT_CODE_FILE" ]; then
    COMMAND_STATUS=$(cat "$EXIT_CODE_FILE")
    rm -f "$EXIT_CODE_FILE"
  else
    COMMAND_STATUS=1
  fi

  # プロセス終了時のSlack通知
  if [ "$COMMAND_STATUS" -eq 0 ]; then
    STATUS_COLOR="#2eb886"
    RESULT_LABEL="成功"
  else
    STATUS_COLOR="#FF0000"
    RESULT_LABEL="失敗 (exit $COMMAND_STATUS)"
  fi

  END_MSG="${COMMAND_LABEL} が終了したってコト!? (PID： \`$PID\` / ${RESULT_LABEL})"
  LAST_LINES=$(tail -n 5 "$LOG_FILE")
  LOG_PATH_MESSAGE="\`$LOG_FILE\`"
  MESSAGE_DATA="payload={\"username\": \"$USERNAME\", \"text\": \"${END_MSG}\", \"attachments\": [{\"color\": \"$STATUS_COLOR\",\"title\": \"コンソールの最後の5行\",\"text\": \"\`\`\`$LAST_LINES\`\`\`\"},{\"color\": \"#808080\",\"title\": \"フルログパス\",\"text\": \"$LOG_PATH_MESSAGE\"}]}"
  curl -s -X POST --data-urlencode "$MESSAGE_DATA" ${URL} >/dev/null
} &
