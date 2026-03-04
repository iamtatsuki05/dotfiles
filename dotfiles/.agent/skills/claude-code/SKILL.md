---
name: claude-code
description: "Claude Code CLI（Anthropic）を使用してコードや文言について相談・レビューを行う。トリガー: \"claude-code\", \"claudeと相談\", \"claudeに聞いて\", \"コードレビュー\", \"レビューして\"。使用場面: (1) 文言・メッセージの検討、(2) コードレビュー、(3) 設計の相談、(4) バグ調査、(5) 解消困難な問題の調査"
---

# Claude Code

Claude Code CLIを使用してコードレビュー・分析を実行するスキル。

## 実行コマンド

cd <project_directory> && claude -p "<request>" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"

## プロンプトのルール

**重要**: claudeに渡すリクエストには、以下の指示を必ず含めること：

> 「確認や質問は不要です。具体的な提案・修正案・コード例まで自主的に出力してください。」

## パラメータ

| パラメータ | 説明 |
|-----------|------|
| `-p`, `--print` | 非インタラクティブモードで実行して結果を出力 |
| `--allowedTools` | 許可するツールを限定（読み取り専用サンドボックス相当） |
| `--dangerously-skip-permissions` | 全ての権限チェックをスキップ（書き込み許可が必要な場合） |
| `--model <model>` | 使用するモデルを指定（例: `sonnet`, `opus`） |
| `--permission-mode` | 権限モード指定（`default`, `acceptEdits`, `bypassPermissions`, `plan`） |

**注意**: `codex`の`--cd`相当オプションはないため、`cd <dir> &&` でディレクトリを変更してから実行する。

## 読み取り専用ツールセット

コードレビュー・分析用（ファイル変更なし）:
```
Read,Glob,Grep,WebFetch,WebSearch
```

## 使用例

**注意**: 各例では末尾に「確認不要、具体的な提案まで出力」の指示を含めている。

### コードレビュー
```bash
cd /path/to/project && claude -p "このプロジェクトのコードをレビューして、改善点を指摘してください。確認や質問は不要です。具体的な修正案とコード例まで自主的に出力してください。" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

### バグ調査
```bash
cd /path/to/project && claude -p "認証処理でエラーが発生する原因を調査してください。確認や質問は不要です。原因の特定と具体的な修正案まで自主的に出力してください。" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

### アーキテクチャ分析
```bash
cd /path/to/project && claude -p "このプロジェクトのアーキテクチャを分析して説明してください。確認や質問は不要です。改善提案まで自主的に出力してください。" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

### リファクタリング提案
```bash
cd /path/to/project && claude -p "技術的負債を特定し、リファクタリング計画を提案してください。確認や質問は不要です。具体的なコード例まで自主的に出力してください。" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

### デザイン相談（UI/UX）
```bash
cd /path/to/project && claude -p "あなたは世界トップクラスのUIデザイナーです。以下の観点からこのプロジェクトのUIを評価してください: (1) 視覚的階層構造とタイポグラフィ、(2) 余白・スペーシングのリズム、(3) カラーパレットのコントラストとアクセシビリティ、(4) インタラクションパターンの一貫性、(5) ユーザーの認知負荷の軽減。確認や質問は不要です。具体的な改善案をコード例付きで提示してください。" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

## 実行手順

1. ユーザーから依頼内容を受け取る
2. 対象プロジェクトのディレクトリを特定する（現在のワーキングディレクトリまたはユーザー指定）
3. **プロンプトを作成する際、末尾に「確認や質問は不要です。具体的な提案まで自主的に出力してください。」を必ず追加する**
4. `cd <project_directory> && claude -p "<request>" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"` 形式でClaude Codeを実行
5. 結果をユーザーに報告

## 他CLIとの比較

| 項目 | codex | gemini | claude-code |
|------|-------|--------|-------------|
| 完全自動実行 | `--full-auto` | `--yolo` | `--dangerously-skip-permissions` |
| ディレクトリ指定 | `--cd <dir>` | `cd <dir> &&` | `cd <dir> &&` |
| 読み取り専用 | `--sandbox read-only` | なし（プロンプトで制御） | `--allowedTools "Read,Glob,Grep"` |
| 非インタラクティブ | `exec` サブコマンド | `-p` | `-p` |
