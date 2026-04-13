# gws コマンドリファレンス

gws 0.22.5 時点のコマンドリファレンス。

## グローバルフラグ

| フラグ | 説明 |
|-------|------|
| `--params <JSON>` | URL/クエリパラメータを JSON で指定 |
| `--json <JSON>` | リクエストボディを JSON で指定（POST/PATCH/PUT） |
| `--upload <PATH>` | アップロードするローカルファイル |
| `--output <PATH>` | バイナリレスポンスの保存先 |
| `--format <FMT>` | 出力形式: `json`（デフォルト）/ `table` / `yaml` / `csv` |
| `--api-version <VER>` | API バージョンの上書き |
| `--page-all` | 全ページを自動取得（NDJSON） |
| `--page-limit <N>` | `--page-all` 時の最大ページ数（デフォルト: 10） |
| `--dry-run` | API に送信せずにリクエスト内容を検証 |

## 認証

```bash
gws auth setup     # GCP プロジェクト + OAuth クライアントの初期設定
gws auth login     # OAuth2 認証（ブラウザが開く）
gws auth status    # 現在の認証状態を表示
gws auth logout    # 認証情報とトークンキャッシュをクリア
```

## Calendar

### ヘルパーコマンド

| コマンド | 説明 |
|---------|------|
| `gws calendar +agenda` | 全カレンダーの今後の予定を表示 |
| `gws calendar +insert` | 新しいイベントを作成 |

#### +agenda オプション

| オプション | 説明 |
|-----------|------|
| `--today` | 今日の予定 |
| `--tomorrow` | 明日の予定 |
| `--week` | 今週の予定 |
| `--days <N>` | N日分の予定 |
| `--calendar <NAME>` | カレンダー名または ID で絞り込み |
| `--timezone <TZ>` | タイムゾーン指定（例: `Asia/Tokyo`） |

#### +insert オプション

| オプション | 説明 |
|-----------|------|
| `--summary <TEXT>` | イベントタイトル（必須） |
| `--start <TIME>` | 開始日時 ISO 8601（必須） |
| `--end <TIME>` | 終了日時 ISO 8601（必須） |
| `--calendar <ID>` | カレンダー ID（デフォルト: primary） |
| `--location <TEXT>` | 場所 |
| `--description <TEXT>` | 説明 |
| `--attendee <EMAIL>` | 参加者メール（複数指定可） |
| `--meet` | Google Meet リンクを追加 |

### 低レベル API

```bash
# イベント一覧（今後7日間）
gws calendar events list --params '{
  "calendarId": "primary",
  "timeMin": "2026-04-13T00:00:00+09:00",
  "timeMax": "2026-04-20T00:00:00+09:00",
  "singleEvents": true,
  "orderBy": "startTime"
}'

# イベント取得
gws calendar events get --params '{"calendarId": "primary", "eventId": "EVENT_ID"}'

# イベント削除
gws calendar events delete --params '{"calendarId": "primary", "eventId": "EVENT_ID"}'

# カレンダー一覧
gws calendar calendarList list
```

## Drive

### ヘルパーコマンド

| コマンド | 説明 |
|---------|------|
| `gws drive +upload <file>` | ファイルをアップロード（MIME タイプ自動検出） |

#### +upload オプション

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
# ファイル一覧
gws drive files list --params '{"pageSize": 20, "fields": "files(id,name,mimeType,modifiedTime)"}'

# ファイル情報取得
gws drive files get --params '{"fileId": "FILE_ID"}'

# バイナリダウンロード
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}' --output ./file.pdf

# ファイル削除（ゴミ箱へ）
gws drive files delete --params '{"fileId": "FILE_ID"}'
```

## Gmail

### ヘルパーコマンド

| コマンド | 説明 |
|---------|------|
| `gws gmail +triage` | 未読メールの一覧表示 |
| `gws gmail +read` | メール本文の読み取り |
| `gws gmail +send` | メール送信 |
| `gws gmail +reply` | メール返信 |
| `gws gmail +reply-all` | 全員返信 |
| `gws gmail +forward` | 転送 |
| `gws gmail +watch` | 新着メールをリアルタイム監視（NDJSON） |

#### +triage オプション

| オプション | 説明 |
|-----------|------|
| `--max <N>` | 表示件数（デフォルト: 20） |
| `--query <QUERY>` | Gmail 検索クエリ（デフォルト: `is:unread`） |
| `--labels` | ラベル名を含めて表示 |

#### +send オプション

| オプション | 説明 |
|-----------|------|
| `--to <EMAILS>` | 宛先（カンマ区切りで複数指定可）（必須） |
| `--subject <SUBJECT>` | 件名（必須） |
| `--body <TEXT>` | 本文（必須） |
| `--cc <EMAILS>` | CC |
| `--bcc <EMAILS>` | BCC |
| `--from <EMAIL>` | 送信元（エイリアス用） |
| `-a <PATH>` | 添付ファイル（複数指定可、合計 25MB まで） |
| `--html` | HTML メールとして送信 |
| `--draft` | 下書きとして保存 |

### Gmail 検索クエリ例

| 条件 | クエリ |
|-----|-------|
| 未読 | `is:unread` |
| 特定の送信者 | `from:alice@example.com` |
| 件名に含む | `subject:報告書` |
| 添付ファイルあり | `has:attachment` |
| 期間指定 | `after:2026/04/01 before:2026/04/14` |
| スター付き | `is:starred` |

## Tasks

```bash
# タスクリスト一覧
gws tasks tasklists list

# タスク一覧
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
