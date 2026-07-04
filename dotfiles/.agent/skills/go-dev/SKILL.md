---
name: go-dev
description: "Use when the user asks to implement, refactor, test, debug, or review Go code, Go modules, error handling, concurrency, interfaces, generics, or go test/build/vet failures."
---

# Go開発スキル

Goコードの実装、テスト、デバッグ、リファクタリングを効率的に行うためのガイド。

## 実装前の必須確認

**go.mod と Makefile を必ず確認する。** プロジェクトのGoバージョン、依存関係、ビルド手順を把握する。

確認項目:
- `go.mod`: Goバージョン（1.21+推奨）、依存ライブラリ
- `Makefile`: ビルド、テスト、lint コマンド
- `.golangci.yml`: linter設定（存在する場合）
- 既存の package 構成、テストヘルパー、エラー処理、context の使い方
- `Makefile` がない場合は `go test ./...`、`go test ./path`、`go vet ./...` など標準コマンドを確認する

### プロジェクト構造

既存プロジェクトの構成に従う。新規プロジェクトは [references/coding-standards.md](references/coding-standards.md) の標準レイアウト（`cmd/` + `internal/`）を使う。

## コーディング規約

詳細なコード例は [references/coding-standards.md](references/coding-standards.md) を参照。判断基準:

- **インターフェース**: 提供側ではなく使用側の package で必要最小限のメソッドだけ定義する。準拠はコンパイル時に `var _ UserRepository = (*userRepository)(nil)` で確認する。
- **エラー**: `fmt.Errorf("find user %d: %w", id, err)` のように `%w` でラップして文脈を付与する。判定は文字列比較ではなく `errors.Is` / `errors.As` を使う。
- **命名**: パッケージ名は小文字1語でディレクトリ名と一致させる。stuttering（`user.UserService` 等）を避ける。
- **golangci-lint 対応**（errcheck / govet / revive 等）で迷ったら references/coding-standards.md の該当節を参照する。

## テスト

実装例（テーブル駆動、モック、Golden files、ベンチマーク、統合テスト）は [references/testing-guide.md](references/testing-guide.md) を参照。判断基準:

- テーブル駆動テスト + `t.Run` によるサブテストを既定とする。
- `t.Parallel()` を使う場合は、テストケース間の共有状態（グローバル変数、テストデータの使い回し）に注意する。並行系のコードは `go test -race` で確認する。
- テストヘルパーには `t.Helper()` を付け、失敗位置を呼び出し側で報告させる。

## 高度なパターン

詳細なコード例（context、errgroup、セマフォ、ワーカープール、functional options、リトライ等）は [references/common-patterns.md](references/common-patterns.md) を参照。判断基準:

- **context**: 外部 I/O やブロックし得る処理には `context.Context` を第一引数で渡す。タイムアウトやキャンセルは `context.WithTimeout` + `defer cancel()` で呼び出し側が制御する。
- **並行処理**: エラーを返す goroutine 群には `errgroup` を第一候補にする。並行数制限はセマフォ、ストリーム処理はワーカープールや fan-in/fan-out を検討する。
- **functional options**: 省略可能な設定が多いコンストラクタは `Option func(*Config)` パターンでデフォルト値 + 可変長オプションにする。

最小例（errgroup、Go 1.22+ 前提。Go 1.21 以前ではループ変数の再宣言 `url := url` が必要）:

```go
import "golang.org/x/sync/errgroup"

g, ctx := errgroup.WithContext(ctx)
for _, url := range urls {
    g.Go(func() error { return fetch(ctx, url) })
}
if err := g.Wait(); err != nil {
    return err
}
```

## エンジニアリング作法（共通）

Small CL、テスト同梱、Why コメント、PR description の共通規範は `eng-practices` スキルを参照する。
Go では特に、機能変更に対応する `_test.go` の追加・更新を同じ PR に含めることを徹底する。

## コード品質チェック

実装後に確認:
- `go build ./...` を通過するか
- `go test ./...` が通過するか
- `go vet ./...` で警告がないか
- `golangci-lint run`（設定がある場合）
- 変更範囲が狭い場合は該当 package のテストを先に実行し、最後に可能な範囲で全体確認する
- 実行不能な検証があれば、理由と代替確認を報告する

## リファレンス

詳細なガイドは以下を参照:

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md) — 標準レイアウト、命名規則、エラーハンドリング、インターフェース設計、golangci-lint 対応の実装例が必要なとき
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md) — テーブル駆動テスト、モック、Golden files、ベンチマーク、統合テストの実装例が必要なとき
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md) — errgroup / セマフォ / ワーカープール、functional options、リトライ、HTTP / DB パターンの実装例が必要なとき
- **標準ライブラリ早見表**（context / errors / io / net/http / encoding/json / sync / time / log/slog）: [references/api_reference.md](references/api_reference.md) — 標準ライブラリの API シグネチャや定番の使い方を確認したいとき
