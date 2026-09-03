# gws コマンドリファレンス

gws 0.22.5 時点。helper の help は `gws <service> help +<name>` で表示できる。

## グローバルフラグ

| フラグ | 説明 |
|-------|------|
| `--params <JSON>` | URL/クエリパラメータを JSON で指定 |
| `--json <JSON>` | リクエストボディを JSON で指定（POST/PATCH/PUT） |
| `--upload <PATH>` | アップロードするローカルファイル |
| `--upload-content-type <MIME>` | アップロードファイルの MIME type（省略時は拡張子から自動判定） |
| `--output <PATH>` | バイナリレスポンスの保存先（current directory 配下のみ） |
| `--format <FMT>` | 出力形式: `json`（デフォルト）/ `table` / `yaml` / `csv` |
| `--api-version <VER>` | API バージョンの上書き |
| `--page-all` | 全ページを自動取得（NDJSON） |
| `--page-limit <N>` | `--page-all` 時の最大ページ数（デフォルト: 10） |
| `--page-delay <MS>` | ページ取得間の待機 ms（デフォルト: 100） |
| `--dry-run` | API に送信せずにリクエスト内容を表示（token 取得は行う） |

## 認証

```bash
gws auth setup     # GCP プロジェクト + OAuth クライアントの初期設定（gcloud が必要）
gws auth login     # OAuth2 認証（ブラウザが開く）
gws auth status    # 現在の認証状態を表示
gws auth logout    # 認証情報とトークンキャッシュをクリア
```

## Calendar

### +agenda オプション

| オプション | 説明 |
|-----------|------|
| `--today` | 今日の予定 |
| `--tomorrow` | 明日の予定 |
| `--week` | 今週の予定 |
| `--days <N>` | N日分の予定 |
| `--calendar <NAME>` | カレンダー名または ID で絞り込み（既定は全カレンダー） |
| `--timezone <TZ>` | IANA タイムゾーン（例: `Asia/Tokyo`。既定は Google アカウントの設定） |

### +insert オプション

| オプション | 説明 |
|-----------|------|
| `--summary <TEXT>` | イベントタイトル（必須） |
| `--start <TIME>` | 開始日時 RFC3339（必須） |
| `--end <TIME>` | 終了日時 RFC3339（必須） |
| `--calendar <ID>` | カレンダー ID（デフォルト: primary） |
| `--location <TEXT>` | 場所 |
| `--description <TEXT>` | 説明 |
| `--attendee <EMAIL>` | 参加者メール（複数指定可） |
| `--meet` | Google Meet リンクを追加 |

### 低レベル API

```bash
# イベント一覧（期間指定）
gws calendar events list --params '{
  "calendarId": "primary",
  "timeMin": "2026-04-13T00:00:00+09:00",
  "timeMax": "2026-04-20T00:00:00+09:00",
  "singleEvents": true,
  "orderBy": "startTime"
}'

# イベント取得 / 削除
gws calendar events get --params '{"calendarId": "primary", "eventId": "EVENT_ID"}'
gws calendar events delete --params '{"calendarId": "primary", "eventId": "EVENT_ID"}'

# カレンダー一覧
gws calendar calendarList list
```

## Drive

### +upload オプション

| オプション | 説明 |
|-----------|------|
| `--parent <ID>` | 親フォルダの ID |
| `--name <NAME>` | アップロード後のファイル名（省略時はローカルファイル名） |

### ファイル検索クエリ（`q` パラメータ）

| 条件 | クエリ例 |
|-----|---------|
| 名前に含む | `name contains 'キーワード'` |
| 特定フォルダ内 | `'FOLDER_ID' in parents` |
| フォルダのみ | `mimeType = 'application/vnd.google-apps.folder'` |
| Google ドキュメントのみ | `mimeType = 'application/vnd.google-apps.document'` |
| ゴミ箱以外 | `trashed = false` |
| 複合条件 | `name contains 'report' and trashed = false` |

### 低レベル API

```bash
gws drive files list --params '{"pageSize": 20, "fields": "files(id,name,mimeType,modifiedTime)"}'
gws drive files get --params '{"fileId": "FILE_ID"}'
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}' --output ./file.pdf
gws drive files delete --params '{"fileId": "FILE_ID"}'     # ゴミ箱へ
```

## Gmail

### ヘルパー一覧

| コマンド | 説明 |
|---------|------|
| `gws gmail +triage` | 未読メールの一覧表示（read-only） |
| `gws gmail +read --id <ID>` | メール本文の読み取り |
| `gws gmail +send` | メール送信 |
| `gws gmail +reply --message-id <ID>` | 返信（スレッド処理は自動） |
| `gws gmail +reply-all --message-id <ID>` | 全員返信 |
| `gws gmail +forward --message-id <ID> --to <EMAILS>` | 転送 |
| `gws gmail +watch` | 新着メールをリアルタイム監視（NDJSON） |

### +triage オプション

| オプション | 説明 |
|-----------|------|
| `--max <N>` | 表示件数（デフォルト: 20） |
| `--query <QUERY>` | Gmail 検索クエリ（デフォルト: `is:unread`） |
| `--labels` | ラベル名を含めて表示 |

### +read オプション

| オプション | 説明 |
|-----------|------|
| `--id <ID>` | メッセージ ID（必須） |
| `--headers` | From / To / Subject / Date を含める |
| `--format <text\|json>` | 出力形式（デフォルト: text） |
| `--html` | HTML 本文をそのまま返す |

### +send / +reply オプション

| オプション | 説明 |
|-----------|------|
| `--to <EMAILS>` | 宛先（カンマ区切り。`+send` では必須、`+reply` では追加宛先） |
| `--subject <SUBJECT>` | 件名（`+send` のみ、必須） |
| `--body <TEXT>` | 本文（必須） |
| `--message-id <ID>` | 返信元メッセージ ID（`+reply` / `+reply-all` / `+forward` で必須） |
| `--cc <EMAILS>` / `--bcc <EMAILS>` | CC / BCC |
| `--from <EMAIL>` | 送信元（send-as エイリアス） |
| `-a, --attach <PATH>` | 添付ファイル（複数指定可、合計 25MB まで） |
| `--html` | 本文を HTML として送信（`<p>` などの断片で書く） |
| `--draft` | 送信せず下書きとして保存 |
| `--dry-run` | 送信内容を表示して終了 |

### Gmail 検索クエリ例

| 条件 | クエリ |
|-----|-------|
| 未読 | `is:unread` |
| 特定の送信者 | `from:alice@example.com` |
| 件名に含む | `subject:報告書` |
| 添付ファイルあり | `has:attachment` |
| 期間指定 | `after:2026/04/01 before:2026/04/14` |
| スター付き | `is:starred` |

### 低レベル API

```bash
gws gmail users messages list --params '{"userId": "me", "q": "is:unread", "maxResults": 10}'
gws gmail users messages get --params '{"userId": "me", "id": "MESSAGE_ID", "format": "metadata"}'
```

## Tasks

```bash
# タスクリスト一覧 / タスク一覧
gws tasks tasklists list
gws tasks tasks list --params '{"tasklist": "TASKLIST_ID"}'

# タスク作成
gws tasks tasks insert \
  --params '{"tasklist": "TASKLIST_ID"}' \
  --json '{"title": "タスク名", "notes": "メモ", "due": "2026-04-20T00:00:00.000Z"}'

# タスク完了
gws tasks tasks patch \
  --params '{"tasklist": "TASKLIST_ID", "task": "TASK_ID"}' \
  --json '{"status": "completed"}'
```

## 環境変数

| 変数 | 説明 |
|-----|------|
| `GOOGLE_WORKSPACE_CLI_TOKEN` | 取得済み OAuth2 アクセストークン（最優先） |
| `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` | OAuth 認証情報 JSON のパス |
| `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` | 設定ディレクトリの上書き（デフォルト: `~/.config/gws`） |
| `GOOGLE_WORKSPACE_CLI_LOG` | ログレベル（例: `gws=debug`） |
