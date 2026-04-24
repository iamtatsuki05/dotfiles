---
name: codex
description: "Use when the user explicitly wants to consult Codex CLI for review, wording, design, debugging, architecture, or difficult code investigation, including requests such as codex, codex と相談, codex に聞いて, コードレビュー, or レビューして."
---

# Codex

Codex CLIを使用してコードレビュー・分析を実行するスキル。

## 実行コマンド

相談・レビュー・調査の既定:

```bash
codex exec --sandbox read-only --cd <project_directory> "<request>"
```

実装まで任せる場合:

```bash
codex exec --full-auto --sandbox workspace-write --cd <project_directory> "<request>"
```

## プロンプトのルール

**重要**: codexに渡すリクエストには、以下の指示を必ず含めること：

> 「確認や質問は不要です。具体的な提案・修正案・コード例まで自主的に出力してください。」

## パラメータ

| パラメータ | 説明 |
|-----------|------|
| `--full-auto` | 完全自動モードで実行 |
| `--sandbox read-only` | 読み取り専用。相談・レビューの既定 |
| `--sandbox workspace-write` | ワークスペース内で書き込み可能なサンドボックス |
| `--cd <dir>` | 対象プロジェクトのディレクトリ |
| `"<request>"` | 依頼内容（日本語可） |

## 安全弁と検証

- 相談・レビュー・調査では `--sandbox read-only` を既定にする。`workspace-write` と `--full-auto` は、ユーザーが実装を依頼し、書き込み対象が明確な場合だけ使う。
- 実装を任せた場合でも、Codex CLI の変更は採用前に `git diff` 相当で確認し、テスト・lint・型チェックなどプロジェクトの検証を走らせる。
- CLI 出力は提案として扱う。根拠のない断定や大きな設計変更は、自分でコードと要件に照らしてから報告する。
- 失敗時は終了コード、主要なエラー、再実行に必要な権限・前提を報告する。権限拡大や追加インストールはユーザー確認後に行う。

## 使用例

**注意**: 各例では末尾に「確認不要、具体的な提案まで出力」の指示を含めている。

### コードレビュー
codex exec --sandbox read-only --cd /path/to/project "このプロジェクトのコードをレビューして、改善点を指摘してください。確認や質問は不要です。具体的な修正案とコード例まで自主的に出力してください。"

### バグ調査
codex exec --sandbox read-only --cd /path/to/project "認証処理でエラーが発生する原因を調査してください。確認や質問は不要です。原因の特定と具体的な修正案まで自主的に出力してください。"

### アーキテクチャ分析
codex exec --sandbox read-only --cd /path/to/project "このプロジェクトのアーキテクチャを分析して説明してください。確認や質問は不要です。改善提案まで自主的に出力してください。"

### リファクタリング提案
codex exec --sandbox read-only --cd /path/to/project "技術的負債を特定し、リファクタリング計画を提案してください。確認や質問は不要です。具体的なコード例まで自主的に出力してください。"

### デザイン相談（UI/UX）
codex exec --sandbox read-only --cd /path/to/project "あなたは世界トップクラスのUIデザイナーです。以下の観点からこのプロジェクトのUIを評価してください: (1) 視覚的階層構造とタイポグラフィ、(2) 余白・スペーシングのリズム、(3) カラーパレットのコントラストとアクセシビリティ、(4) インタラクションパターンの一貫性、(5) ユーザーの認知負荷の軽減。確認や質問は不要です。具体的な改善案をコード例付きで提示してください。"

codex exec --sandbox read-only --cd /path/to/project "UXリサーチャー兼デザイナーとして、このフォームのユーザビリティを分析してください。Nielsen の10ヒューリスティクスに基づき、(1) エラー防止の仕組み、(2) ユーザーの制御と自由度、(3) 一貫性と標準、(4) 認識vs記憶の負荷、(5) 柔軟性と効率性を評価してください。確認や質問は不要です。具体的な改善案まで自主的に提示してください。"

## 実行手順

1. ユーザーから依頼内容を受け取る
2. 対象プロジェクトのディレクトリを特定する（現在のワーキングディレクトリまたはユーザー指定）
3. **プロンプトを作成する際、末尾に「確認や質問は不要です。具体的な提案まで自主的に出力してください。」を必ず追加する**
4. 相談・レビューなら `read-only`、実装依頼なら `workspace-write` で Codex を実行
5. 出力や差分を自分で確認し、採用できる点・保留すべき点・追加検証が必要な点を分けて報告
