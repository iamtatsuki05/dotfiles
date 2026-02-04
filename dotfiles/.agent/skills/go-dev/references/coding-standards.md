# コーディング規約詳細

## 目次

1. [プロジェクト設定](#プロジェクト設定)
2. [命名規則](#命名規則)
3. [パッケージ構成](#パッケージ構成)
4. [エラーハンドリング](#エラーハンドリング)
5. [インターフェース設計](#インターフェース設計)
6. [golangci-lint対応](#golangci-lint対応)

## プロジェクト設定

### go.mod

```go
module example.com/myproject

go 1.22

require (
    github.com/go-chi/chi/v5 v5.0.10
    golang.org/x/sync v0.5.0
)
```

### .golangci.yml（推奨設定）

```yaml
run:
  timeout: 5m

linters:
  enable:
    - errcheck
    - govet
    - staticcheck
    - unused
    - gosimple
    - ineffassign
    - gofmt
    - goimports
    - misspell
    - unconvert
    - gocritic
    - revive

linters-settings:
  goimports:
    local-prefixes: example.com/myproject
  revive:
    rules:
      - name: exported
        arguments:
          - disableStutteringCheck
```

## 命名規則

```go
// パッケージ名: 小文字、短く、単数形
package user     // OK
package users    // NG（複数形避ける）
package userPkg  // NG（接尾辞避ける）

// 型名: PascalCase（公開）、camelCase（非公開）
type UserService struct{}  // 公開
type userCache struct{}    // 非公開

// インターフェース: 動詞+er形が一般的
type Reader interface{}
type Validator interface{}

// 関数/メソッド: PascalCase（公開）、camelCase（非公開）
func ParseConfig() {}    // 公開
func validateInput() {}  // 非公開

// 変数: camelCase
var userCount int
var isValid bool

// 定数: PascalCase（公開）、camelCase（非公開）
const MaxRetries = 3     // 公開
const defaultTimeout = 30 // 非公開

// レシーバ名: 1-2文字、型名の頭文字
func (u *User) Name() string {}
func (us *UserService) Create() {}

// 頭字語は全て大文字または全て小文字
type HTTPClient struct{}  // OK
type HttpClient struct{}  // NG
var userID int64         // OK
var userId int64         // NG
```

## パッケージ構成

### 標準的なレイアウト

```
myproject/
├── cmd/
│   └── server/
│       └── main.go
├── internal/
│   ├── domain/
│   │   └── user/
│   │       ├── user.go
│   │       └── repository.go
│   ├── handler/
│   │   └── user_handler.go
│   └── infra/
│       └── postgres/
│           └── user_repository.go
├── pkg/
│   └── validator/
│       └── validator.go
├── go.mod
└── go.sum
```

### インポート順序

```go
import (
    // 1. 標準ライブラリ
    "context"
    "errors"
    "fmt"
    "net/http"

    // 2. サードパーティ
    "github.com/go-chi/chi/v5"
    "go.uber.org/zap"

    // 3. 自プロジェクト
    "example.com/myproject/internal/domain"
    "example.com/myproject/pkg/validator"
)
```

## エラーハンドリング

### 基本パターン

```go
// エラーは必ずチェック
result, err := doSomething()
if err != nil {
    return fmt.Errorf("do something: %w", err)
}

// 複数の戻り値がある場合
user, found, err := repo.Find(ctx, id)
if err != nil {
    return nil, fmt.Errorf("find user: %w", err)
}
if !found {
    return nil, ErrUserNotFound
}
```

### カスタムエラー

```go
// センチネルエラー
var (
    ErrNotFound     = errors.New("not found")
    ErrUnauthorized = errors.New("unauthorized")
)

// 構造体エラー（追加情報が必要な場合）
type ValidationError struct {
    Field   string
    Message string
}

func (e *ValidationError) Error() string {
    return fmt.Sprintf("validation error: %s - %s", e.Field, e.Message)
}

// エラー判定
func IsNotFound(err error) bool {
    return errors.Is(err, ErrNotFound)
}

func AsValidationError(err error) (*ValidationError, bool) {
    var ve *ValidationError
    if errors.As(err, &ve) {
        return ve, true
    }
    return nil, false
}
```

### エラーラップ

```go
// 文脈を追加してラップ
func (s *UserService) Create(ctx context.Context, req CreateRequest) (*User, error) {
    if err := req.Validate(); err != nil {
        return nil, fmt.Errorf("validate request: %w", err)
    }

    user, err := s.repo.Save(ctx, req.ToUser())
    if err != nil {
        return nil, fmt.Errorf("save user: %w", err)
    }

    return user, nil
}
```

## インターフェース設計

### 使用側で定義

```go
// handler/user_handler.go
package handler

// UserServiceは使用側で必要なメソッドのみ定義
type UserService interface {
    Create(ctx context.Context, req CreateRequest) (*domain.User, error)
    Find(ctx context.Context, id int64) (*domain.User, error)
}

type UserHandler struct {
    service UserService
}

func NewUserHandler(service UserService) *UserHandler {
    return &UserHandler{service: service}
}
```

### 小さく保つ

```go
// 大きなインターフェース（避ける）
type UserRepository interface {
    Create(ctx context.Context, user *User) error
    Find(ctx context.Context, id int64) (*User, error)
    Update(ctx context.Context, user *User) error
    Delete(ctx context.Context, id int64) error
    List(ctx context.Context, filter Filter) ([]*User, error)
    Count(ctx context.Context, filter Filter) (int64, error)
}

// 小さなインターフェース（推奨）
type UserCreator interface {
    Create(ctx context.Context, user *User) error
}

type UserFinder interface {
    Find(ctx context.Context, id int64) (*User, error)
}

// 必要に応じて組み合わせ
type UserReadWriter interface {
    UserFinder
    UserCreator
}
```

### 準拠確認

```go
// コンパイル時にインターフェース準拠を確認
var _ UserRepository = (*postgresUserRepository)(nil)
var _ io.Reader = (*MyReader)(nil)
```

## golangci-lint対応

### errcheck

```go
// NG: エラーを無視
json.Marshal(data)

// OK: エラーをチェック
bytes, err := json.Marshal(data)
if err != nil {
    return err
}

// OK: 明示的に無視（必要な場合のみ）
_ = file.Close()
```

### govet

```go
// NG: Printf形式の不一致
fmt.Printf("id: %s", 123)  // %sに数値

// OK
fmt.Printf("id: %d", 123)
```

### ineffassign

```go
// NG: 使用されない代入
x := 10
x = 20  // 上の代入は不要

// OK
x := 20
```

### unconvert

```go
// NG: 不要な型変換
var i int = 10
j := int(i)  // 不要

// OK
j := i
```

### revive (exported)

```go
// NG: エクスポートされた型のドキュメントなし
type User struct{}

// OK
// User represents a user in the system.
type User struct{}
```
