---
name: gws
description: "Use when the user asks to inspect or operate Google Calendar, Drive, Gmail, or Tasks through the gws CLI: agendas, event creation, Drive search/upload/download, Gmail triage/read/send/reply, task lists. Do not use for other Google services, for browser UI work, or when the user has not asked to touch their Google account."
---

# Google Workspace CLI (gws)

`gws`(0.22.5 で確認)で Google Calendar・Drive・Gmail・Tasks を操作する。helper(`+agenda` など)を第一選択にし、helper が無い操作は低レベル API(`gws <service> <resource> <method> --params '<JSON>'`)を使う。

## 事前確認

```bash
command -v gws     # 無ければ missing-tools skill
gws auth status
```

未認証や `invalid_grant`(token 失効)は `gws auth login`(ブラウザが開く)をユーザーに案内し、勝手に実行しない。

## コマンドの調べ方

- option を探す順は、このファイル → [references/commands.md](references/commands.md)(全 option 表、検索クエリ、低レベル API、環境変数)→ 最後に help。
- helper の help は `gws <service> help +<name>`(例: `gws gmail help +send`)。`gws gmail +send --help` の形は `Unknown service` エラーになる。
- `--dry-run` はリクエストを送らずに内容を表示する。ただし token 取得は走るので認証は必要。
- `--output <PATH>` は current directory 配下しか指定できない。
- 表示は `--format table`(一覧)か `--format json | jq`(絞り込み)を使う。

## 安全弁

- 書き込み(予定作成、送信・返信、upload、削除、Tasks 変更)は、対象アカウント、宛先・参加者、日時、本文、ファイル名、実行コマンドを提示してユーザー承認を得てから実行する。`--dry-run` がある操作は先に dry-run 結果を見せる。
- メールは `--draft` で下書き保存できる場合はそちらを優先する。
- 「今日」「明日」「来週」は現在日付とタイムゾーンで絶対日付に直して確認する。
- メール本文、予定詳細、ファイル名は個人情報を含むため、報告では必要な範囲だけ要約する。

## Calendar

```bash
gws calendar +agenda                          # 直近の予定(全カレンダー、read-only)
gws calendar +agenda --today --format table
gws calendar +agenda --tomorrow
gws calendar +agenda --week --format table
gws calendar +agenda --days 3 --calendar 'Work' --timezone Asia/Tokyo

gws calendar +insert --summary 'レビュー' \
  --start '2026-04-14T14:00:00+09:00' --end '2026-04-14T15:00:00+09:00' \
  --location '会議室A' --description '週次レビュー' \
  --attendee alice@example.com --attendee bob@example.com --meet --dry-run
```

`--start` / `--end` は RFC3339(`2026-04-14T10:00:00+09:00`)。`--calendar <ID>` の既定は `primary`。承認後に `--dry-run` を外して実行する。

## Drive

```bash
gws drive files list --params '{"pageSize": 10}' --format table
gws drive files list --params '{"q": "name contains '\''報告書'\'' and trashed = false"}' --format table
gws drive files list --params '{"q": "'\''FOLDER_ID'\'' in parents"}'
gws drive files list --page-all                                    # 全ページ(NDJSON)

gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}' --output ./downloaded.pdf

gws drive +upload ./report.pdf --parent FOLDER_ID --name '2026-04_report.pdf'
```

## Gmail

```bash
gws gmail +triage                                  # 未読 20 件(read-only)
gws gmail +triage --max 10 --query 'from:boss@example.com' --labels
gws gmail +triage --format json | jq '.[].subject'

gws gmail +read --id MESSAGE_ID --headers          # 本文 + From/To/Subject/Date
gws gmail +read --id MESSAGE_ID --format json | jq '.body'

gws gmail +send --to alice@example.com --subject 'ご連絡' --body 'お世話になっております。' --draft
gws gmail +send --to alice@example.com --cc bob@example.com --subject 'Report' --body 'See attached' -a report.pdf

gws gmail +reply --message-id MESSAGE_ID --body '承知いたしました。' --draft
```

`+reply` の ID 指定は `--message-id`(`--id` ではない)。`+reply-all` / `+forward` も同じ形。`--draft` を外すと即送信になる。

## Tasks

```bash
gws tasks tasklists list --format table
gws tasks tasks list --params '{"tasklist": "TASKLIST_ID"}' --format table
```

作成・完了は低レベル API(`tasks insert` / `tasks patch`)。形は commands.md の Tasks 節。

## 手順

1. `gws auth status` で認証を確認する。
2. 読み取り(`+agenda`、`+triage`、`+read`、`files list`、`tasks list`)は即実行し、結果を整理して報告する。
3. 書き込みは内容を提示し、`--dry-run` か `--draft` で確認してから承認後に実行する。実行後は message id、event id、fileId など識別子と対象を報告する。
