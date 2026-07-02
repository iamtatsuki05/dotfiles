---
name: agent-cli-consult
description: "Use when the user explicitly wants to consult an external agent CLI (Codex CLI or Claude Code CLI) for review, wording, design, debugging, architecture, or difficult code investigation, including requests such as codex, claude-code, codex と相談, claude に聞いて, コードレビュー, or レビューして."
---

# Agent CLI Consult

外部の agent CLI（Codex CLI / Claude Code CLI）にレビュー・分析・調査を依頼する skill。どの CLI を使うかはユーザー指定に従う。指定がない場合は、現在自分が動いている agent とは別系統の CLI を選び、選択理由を一言添える。

## 実行コマンド

相談・レビュー・調査の既定（読み取り専用）:

```bash
# Codex CLI
codex exec --sandbox read-only --cd <project_directory> "<request>"

# Claude Code CLI（--cd 相当が無いので cd してから実行）
cd <project_directory> && claude -p "<request>" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

実装まで任せる場合（ユーザーが実装を依頼し、書き込み対象が明確な場合のみ）:

```bash
# Codex CLI
codex exec --sandbox workspace-write --cd <project_directory> "<request>"

# Claude Code CLI
cd <project_directory> && claude -p "<request>" --permission-mode acceptEdits
```

## プロンプトのルール

**重要**: CLI に渡すリクエストの末尾には、以下の指示を必ず含めること。

> 「確認や質問は不要です。具体的な提案・修正案・コード例まで自主的に出力してください。」

## パラメータ対応表

| 項目 | codex | claude-code |
|------|-------|-------------|
| 非インタラクティブ実行 | `exec` サブコマンド | `-p`, `--print` |
| 読み取り専用 | `--sandbox read-only` | `--allowedTools "Read,Glob,Grep,WebFetch,WebSearch"` |
| 書き込み許可 | `--sandbox workspace-write` | `--permission-mode acceptEdits` |
| 完全自動実行 | `--full-auto`（workspace-write を含意） | `--dangerously-skip-permissions` |
| ディレクトリ指定 | `--cd <dir>` | `cd <dir> &&` |
| モデル指定 | `-m <model>` | `--model <model>` |

## 安全弁と検証

- 相談・レビュー・調査では読み取り専用（Codex は `--sandbox read-only`、Claude Code は読み取り系 `--allowedTools`）を既定にする。書き込み可能なモードや `--full-auto` / `--dangerously-skip-permissions` は、ユーザーが実装まで依頼し、対象範囲が明確な場合だけ使う。
- WebFetch / WebSearch を許可するのは、外部情報が必要な調査に限る。機密コードや未公開情報を外部検索のクエリへ含めない。
- CLI 出力は提案として扱う。採用前に自分で該当ファイル・差分・テスト観点を確認し、根拠のない断定や大きな設計変更はそのまま報告しない。
- 実装を任せた場合は、変更を `git diff` 相当で確認し、テスト・lint・型チェックなどプロジェクトの検証を走らせてから採否を決める。
- CLI が失敗した場合は、終了コード、主要なエラー、再実行に必要な権限・前提を短く報告する。権限拡大や追加インストールはユーザー確認後に行う。

## 使用例

リクエスト本文は CLI 間で共通に使える。末尾に「確認や質問は不要です。具体的な提案まで自主的に出力してください。」を必ず付ける。

- **コードレビュー**: 「このプロジェクトのコードをレビューして、改善点を指摘してください。」
- **バグ調査**: 「認証処理でエラーが発生する原因を調査してください。原因の特定と具体的な修正案まで出力してください。」
- **アーキテクチャ分析**: 「このプロジェクトのアーキテクチャを分析して説明し、改善提案まで出力してください。」
- **リファクタリング提案**: 「技術的負債を特定し、リファクタリング計画を具体的なコード例付きで提案してください。」
- **デザイン相談（UI/UX）**: 「あなたは世界トップクラスのUIデザイナーです。(1) 視覚的階層構造とタイポグラフィ、(2) 余白・スペーシングのリズム、(3) カラーパレットのコントラストとアクセシビリティ、(4) インタラクションパターンの一貫性、(5) ユーザーの認知負荷の軽減、の観点でこのプロジェクトのUIを評価し、改善案をコード例付きで提示してください。」
- **ユーザビリティ分析**: 「UXリサーチャー兼デザイナーとして、このフォームのユーザビリティを Nielsen の10ヒューリスティクスに基づき分析してください。(1) エラー防止の仕組み、(2) ユーザーの制御と自由度、(3) 一貫性と標準、(4) 認識vs記憶の負荷、(5) 柔軟性と効率性を評価し、具体的な改善案まで提示してください。」

実行例:

```bash
codex exec --sandbox read-only --cd /path/to/project "このプロジェクトのコードをレビューして、改善点を指摘してください。確認や質問は不要です。具体的な修正案とコード例まで自主的に出力してください。"

cd /path/to/project && claude -p "認証処理でエラーが発生する原因を調査してください。確認や質問は不要です。原因の特定と具体的な修正案まで自主的に出力してください。" --allowedTools "Read,Glob,Grep,WebFetch,WebSearch"
```

## 実行手順

1. ユーザーの依頼内容と、使う CLI（codex / claude-code、未指定なら別系統の CLI）を確定する
2. 対象プロジェクトのディレクトリを特定する（現在のワーキングディレクトリまたはユーザー指定）
3. プロンプト末尾に「確認や質問は不要です。具体的な提案まで自主的に出力してください。」を必ず追加する
4. 相談・レビューなら読み取り専用、実装依頼なら書き込み可能モードで実行する
5. 出力や差分を自分で確認し、採用できる点・保留すべき点・追加検証が必要な点を分けて報告する
