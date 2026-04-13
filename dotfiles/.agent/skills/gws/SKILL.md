---
name: gws
description: "Google Workspace CLI（gws）を使って Calendar・Drive・Gmail・Tasks を操作するスキル。「カレンダーを確認して」「今日の予定は？」「今週のスケジュール」「ドライブのファイルを探して」「Driveにアップロード」「メールを送って」「受信トレイを確認」「タスクを見せて」などのリクエストでトリガー。認証が必要な場合は gws auth login を案内する。"
---

# Google Workspace CLI (gws)

## Overview

`gws` コマンドを使って Google Calendar・Drive・Gmail・Tasks などの Google Workspace サービスをターミナルから操作するスキル。

## 前提条件：認証状態の確認

作業前に必ず認証状態を確認する。

```bash
gws auth status
```

未認証またはトークン切れの場合:

```bash
gws auth login   # ブラウザが開いて OAuth2 認証
```

---

## Calendar

### 予定の確認（+agenda）

```bash
# 直近の予定（デフォルト）
gws calendar +agenda

# 今日の予定
gws calendar +agenda --today

# 明日の予定
gws calendar +agenda --tomorrow

# 今週の予定（表形式）
gws calendar +agenda --week --format table

# N日分
gws calendar +agenda --days 7

# 特定カレンダーのみ
gws calendar +agenda --today --calendar 'Work'

# タイムゾーン指定
gws calendar +agenda --week --timezone Asia/Tokyo
```

### 予定の作成（+insert）

```bash
# 基本
gws calendar +insert \
  --summary 'ミーティング' \
  --start '2026-04-14T10:00:00+09:00' \
  --end '2026-04-14T11:00:00+09:00'

# 場所・説明・参加者付き
gws calendar +insert \
  --summary 'レビュー' \
  --start '2026-04-14T14:00:00+09:00' \
  --end '2026-04-14T15:00:00+09:00' \
  --location '会議室A' \
  --description '週次レビュー' \
  --attendee alice@example.com \
  --attendee bob@example.com

# Google Meet リンク付き
gws calendar +insert \
  --summary 'オンライン打ち合わせ' \
  --start '2026-04-15T09:00:00+09:00' \
  --end '2026-04-15T09:30:00+09:00' \
  --meet

# 実行前に確認（dry-run）
gws calendar +insert --summary 'Test' \
  --start '2026-04-14T10:00:00+09:00' \
  --end '2026-04-14T11:00:00+09:00' --dry-run
```

> 時刻は ISO 8601 / RFC3339 形式（例: `2026-04-14T10:00:00+09:00`）で指定する。

---

## Drive

### ファイル一覧

```bash
# 直近ファイルを10件
gws drive files list --params '{"pageSize": 10}'

# 名前で検索
gws drive files list --params '{"q": "name contains '\''報告書'\''"}'

# 特定フォルダ内
gws drive files list --params '{"q": "'\''FOLDER_ID'\'' in parents"}'

# 表形式で表示
gws drive files list --params '{"pageSize": 20}' --format table

# 全件取得（ページネーション自動）
gws drive files list --page-all
```

### ファイルのアップロード（+upload）

```bash
# 基本
gws drive +upload ./report.pdf

# フォルダ指定
gws drive +upload ./report.pdf --parent FOLDER_ID

# ファイル名を変えてアップロード
gws drive +upload ./data.csv --name '2026年4月_データ.csv'
```

### ファイルのダウンロード

```bash
# ファイルを取得（バイナリ）
gws drive files get --params '{"fileId": "FILE_ID", "alt": "media"}' --output ./downloaded.pdf
```

---

## Gmail

### 受信トレイの確認（+triage）

```bash
# 未読メールの一覧（デフォルト20件）
gws gmail +triage

# 件数を絞る
gws gmail +triage --max 10

# 検索クエリ指定
gws gmail +triage --query 'from:boss@example.com'

# JSON で取得して jq で絞り込む
gws gmail +triage --format json | jq '.[].subject'

# ラベルも表示
gws gmail +triage --labels
```

### メールを読む（+read）

```bash
gws gmail +read --id MESSAGE_ID
```

### メール送信（+send）

```bash
# 基本
gws gmail +send \
  --to alice@example.com \
  --subject 'ご連絡' \
  --body 'お世話になっております。'

# CC / BCC
gws gmail +send \
  --to alice@example.com \
  --cc bob@example.com \
  --subject '共有' \
  --body '内容をご確認ください。'

# 添付ファイル
gws gmail +send \
  --to alice@example.com \
  --subject '報告書' \
  --body 'ご査収ください。' \
  -a report.pdf

# HTML メール
gws gmail +send \
  --to alice@example.com \
  --subject 'お知らせ' \
  --body '<b>重要</b>なお知らせです。' \
  --html

# 下書き保存
gws gmail +send \
  --to alice@example.com \
  --subject '下書き' \
  --body '確認後に送信予定' \
  --draft
```

### 返信（+reply）

```bash
gws gmail +reply --id MESSAGE_ID --body '承知いたしました。'
```

---

## Tasks

### タスク一覧

```bash
# タスクリストの確認
gws tasks tasklists list

# 特定タスクリストのタスク一覧
gws tasks tasks list --params '{"tasklist": "TASKLIST_ID"}'
```

---

## ワークフロー

### 今日のスケジュールを確認する

1. 認証状態を確認: `gws auth status`
2. 予定を取得: `gws calendar +agenda --today --format table`
3. 内容をユーザーにわかりやすく整理して報告する

### 予定を作成する

1. ユーザーから日時・件名・参加者などを確認する
2. 必要なら `gws calendar +agenda` で空き時間を確認する
3. `--dry-run` で確認してからユーザーに提示する
4. ユーザーの承認後に `--dry-run` なしで実行する

### Drive でファイルを探す

1. `gws drive files list --params '{"q": "name contains '\''キーワード'\''"}' --format table` で検索する
2. 必要なら `fileId` を取得してダウンロードする

## 詳細リファレンス

コマンドオプションの全一覧は [references/commands.md](references/commands.md) を参照。
