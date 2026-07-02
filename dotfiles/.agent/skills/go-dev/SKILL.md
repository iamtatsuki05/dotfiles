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

### プロジェクト構造例

```
project/
├── src/                    # メインソースコード
│   ├── main.go
│   └── project/
│       ├── config/
│       ├── common/utils/
│       ├── env.go
│       └── env_test.go
├── config/                 # 設定ファイル
├── docker/                 # Dockerfile等
├── docs/                   # ドキュメント
├── go.mod
├── go.sum
├── Makefile
└── compose.yml
```

## コーディング規約

### 基本スタイル

```go
// パッケージ名はディレクトリ名と一致、小文字のみ
package user

import (
    "context"
    "errors"
    "fmt"
)

// 公開型はPascalCase、非公開はcamelCase
type User struct {
    ID        int64
    Name      string
    Email     string
    CreatedAt time.Time
}

// コンストラクタ関数
func NewUser(name, email string) *User {
    return &User{
        Name:      name,
        Email:     email,
        CreatedAt: time.Now(),
    }
}

// メソッドレシーバは短い名前（1-2文字）
func (u *User) Validate() error {
    if u.Name == "" {
        return errors.New("name is required")
    }
    return nil
}
```

### エラーハンドリング

```go
// エラーは最後の戻り値
func FindUser(ctx context.Context, id int64) (*User, error) {
    user, err := db.Get(ctx, id)
    if err != nil {
        // エラーをラップして文脈を追加
        return nil, fmt.Errorf("find user %d: %w", id, err)
    }
    return user, nil
}

// カスタムエラー型
type NotFoundError struct {
    Resource string
    ID       int64
}

func (e *NotFoundError) Error() string {
    return fmt.Sprintf("%s not found: %d", e.Resource, e.ID)
}

// エラー判定
func HandleError(err error) {
    var notFound *NotFoundError
    if errors.As(err, &notFound) {
        // NotFoundErrorとして処理
    }
}
```

### インターフェース

```go
// インターフェースは使用側で定義
type UserRepository interface {
    Find(ctx context.Context, id int64) (*User, error)
    Save(ctx context.Context, user *User) error
}

// 実装側は暗黙的に満たす
type userRepository struct {
    db *sql.DB
}

func (r *userRepository) Find(ctx context.Context, id int64) (*User, error) {
    // 実装
}

// インターフェース準拠の確認（コンパイル時）
var _ UserRepository = (*userRepository)(nil)
```

### ジェネリクス（Go 1.18+）

```go
// 型パラメータ
func Filter[T any](items []T, predicate func(T) bool) []T {
    result := make([]T, 0, len(items))
    for _, item := range items {
        if predicate(item) {
            result = append(result, item)
        }
    }
    return result
}

// 型制約
type Number interface {
    ~int | ~int64 | ~float64
}

func Sum[T Number](values []T) T {
    var total T
    for _, v := range values {
        total += v
    }
    return total
}
```

## テスト

```go
package user_test

import (
    "context"
    "testing"

    "example.com/project/user"
)

func TestNewUser(t *testing.T) {
    u := user.NewUser("Alice", "alice@example.com")

    if u.Name != "Alice" {
        t.Errorf("got %q, want %q", u.Name, "Alice")
    }
}

func TestUser_Validate(t *testing.T) {
    tests := []struct {
        name    string
        user    *user.User
        wantErr bool
    }{
        {
            name:    "valid user",
            user:    &user.User{Name: "Alice", Email: "alice@example.com"},
            wantErr: false,
        },
        {
            name:    "empty name",
            user:    &user.User{Name: "", Email: "alice@example.com"},
            wantErr: true,
        },
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            err := tt.user.Validate()
            if (err != nil) != tt.wantErr {
                t.Errorf("Validate() error = %v, wantErr %v", err, tt.wantErr)
            }
        })
    }
}

// ベンチマーク
func BenchmarkFilter(b *testing.B) {
    items := make([]int, 1000)
    for i := range items {
        items[i] = i
    }

    b.ResetTimer()
    for i := 0; i < b.N; i++ {
        Filter(items, func(n int) bool { return n%2 == 0 })
    }
}
```

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

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md)
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md)
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md)
- **標準ライブラリ早見表**（context / errors / io / net/http / encoding/json / sync / time / log/slog）: [references/api_reference.md](references/api_reference.md)
