---
name: go-dev
description: Go開発のための汎用スキル。コード実装、リファクタリング、テスト作成、デバッグ、コードレビューを支援。Goファイル(.go)の作成・編集、go test によるテスト、エラーハンドリング改善、コード品質改善、ベストプラクティス適用時に使用。
---

# Go開発スキル

Goコードの実装、テスト、デバッグ、リファクタリングを効率的に行うためのガイド。

## 実装前の必須確認

**go.mod と Makefile を必ず確認する。** プロジェクトのGoバージョン、依存関係、ビルド手順を把握する。

確認項目:
- `go.mod`: Goバージョン（1.21+推奨）、依存ライブラリ
- `Makefile`: ビルド、テスト、lint コマンド
- `.golangci.yml`: linter設定（存在する場合）

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

### Context活用

```go
func ProcessWithTimeout(ctx context.Context) error {
    ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()

    select {
    case <-ctx.Done():
        return ctx.Err()
    case result := <-doWork(ctx):
        return handleResult(result)
    }
}
```

### 並行処理

```go
func FetchAll(ctx context.Context, urls []string) ([]Result, error) {
    g, ctx := errgroup.WithContext(ctx)
    results := make([]Result, len(urls))

    for i, url := range urls {
        i, url := i, url // ループ変数キャプチャ（Go 1.22以前）
        g.Go(func() error {
            res, err := fetch(ctx, url)
            if err != nil {
                return err
            }
            results[i] = res
            return nil
        })
    }

    if err := g.Wait(); err != nil {
        return nil, err
    }
    return results, nil
}
```

### オプションパターン

```go
type Config struct {
    timeout time.Duration
    retries int
    logger  *slog.Logger
}

type Option func(*Config)

func WithTimeout(d time.Duration) Option {
    return func(c *Config) {
        c.timeout = d
    }
}

func WithRetries(n int) Option {
    return func(c *Config) {
        c.retries = n
    }
}

func NewClient(opts ...Option) *Client {
    cfg := &Config{
        timeout: 30 * time.Second,
        retries: 3,
    }
    for _, opt := range opts {
        opt(cfg)
    }
    return &Client{config: cfg}
}
```

## コード品質チェック

実装後に確認:
- `go build ./...` を通過するか
- `go test ./...` が通過するか
- `go vet ./...` で警告がないか
- `golangci-lint run`（設定がある場合）

## リファレンス

詳細なガイドは以下を参照:

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md)
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md)
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md)
