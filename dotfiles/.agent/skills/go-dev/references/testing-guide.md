# テストガイド

## 目次

1. [テストファイル構成](#テストファイル構成)
2. [基本的なテスト](#基本的なテスト)
3. [テーブル駆動テスト](#テーブル駆動テスト)
4. [フィクスチャとヘルパー](#フィクスチャとヘルパー)
5. [モック](#モック)
6. [並行テスト](#並行テスト)
7. [統合テスト](#統合テスト)
8. [ベンチマーク](#ベンチマーク)

## テストファイル構成

```
myproject/
├── internal/
│   └── user/
│       ├── user.go
│       ├── user_test.go           # 単体テスト（同一パッケージ）
│       ├── user_integration_test.go # 統合テスト
│       └── testdata/              # テストデータ
│           ├── valid_user.json
│           └── invalid_user.json
└── pkg/
    └── validator/
        ├── validator.go
        └── validator_test.go
```

## 基本的なテスト

### シンプルなテスト

```go
package user

import "testing"

func TestNewUser(t *testing.T) {
    u := NewUser("Alice", "alice@example.com")

    if u.Name != "Alice" {
        t.Errorf("Name: got %q, want %q", u.Name, "Alice")
    }
    if u.Email != "alice@example.com" {
        t.Errorf("Email: got %q, want %q", u.Email, "alice@example.com")
    }
}
```

### エラーテスト

```go
func TestValidate_EmptyName(t *testing.T) {
    u := &User{Name: "", Email: "test@example.com"}

    err := u.Validate()

    if err == nil {
        t.Fatal("expected error, got nil")
    }
    if !errors.Is(err, ErrNameRequired) {
        t.Errorf("error: got %v, want %v", err, ErrNameRequired)
    }
}
```

### サブテスト

```go
func TestUser_Validate(t *testing.T) {
    t.Run("valid user", func(t *testing.T) {
        u := &User{Name: "Alice", Email: "alice@example.com"}
        if err := u.Validate(); err != nil {
            t.Errorf("unexpected error: %v", err)
        }
    })

    t.Run("empty name", func(t *testing.T) {
        u := &User{Name: "", Email: "alice@example.com"}
        if err := u.Validate(); err == nil {
            t.Error("expected error, got nil")
        }
    })
}
```

## テーブル駆動テスト

### 基本パターン

```go
func TestUser_Validate(t *testing.T) {
    tests := []struct {
        name    string
        user    *User
        wantErr bool
    }{
        {
            name:    "valid user",
            user:    &User{Name: "Alice", Email: "alice@example.com"},
            wantErr: false,
        },
        {
            name:    "empty name",
            user:    &User{Name: "", Email: "alice@example.com"},
            wantErr: true,
        },
        {
            name:    "empty email",
            user:    &User{Name: "Alice", Email: ""},
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
```

### 期待エラーの検証

```go
func TestParse(t *testing.T) {
    tests := []struct {
        name    string
        input   string
        want    *Config
        wantErr error
    }{
        {
            name:  "valid json",
            input: `{"name":"test"}`,
            want:  &Config{Name: "test"},
        },
        {
            name:    "invalid json",
            input:   `{invalid}`,
            wantErr: ErrInvalidJSON,
        },
        {
            name:    "empty input",
            input:   "",
            wantErr: ErrEmptyInput,
        },
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            got, err := Parse(tt.input)

            if tt.wantErr != nil {
                if !errors.Is(err, tt.wantErr) {
                    t.Errorf("error = %v, wantErr %v", err, tt.wantErr)
                }
                return
            }

            if err != nil {
                t.Fatalf("unexpected error: %v", err)
            }
            if !reflect.DeepEqual(got, tt.want) {
                t.Errorf("got %+v, want %+v", got, tt.want)
            }
        })
    }
}
```

## フィクスチャとヘルパー

### テストヘルパー

```go
// testutil/helper.go
package testutil

import "testing"

func NewTestUser(t *testing.T) *User {
    t.Helper()
    return &User{
        ID:    1,
        Name:  "Test User",
        Email: "test@example.com",
    }
}

func AssertNoError(t *testing.T, err error) {
    t.Helper()
    if err != nil {
        t.Fatalf("unexpected error: %v", err)
    }
}

func AssertError(t *testing.T, err, want error) {
    t.Helper()
    if !errors.Is(err, want) {
        t.Errorf("error = %v, want %v", err, want)
    }
}
```

### testdata

```go
func TestLoadConfig(t *testing.T) {
    // testdata/ディレクトリからファイルを読み込む
    data, err := os.ReadFile("testdata/config.json")
    if err != nil {
        t.Fatalf("failed to read test data: %v", err)
    }

    cfg, err := LoadConfig(data)
    if err != nil {
        t.Fatalf("LoadConfig: %v", err)
    }

    // 検証...
}
```

### Golden files

```go
func TestRender(t *testing.T) {
    got := Render(testData)

    goldenFile := "testdata/render.golden"

    if *update {
        // -update フラグで golden file を更新
        os.WriteFile(goldenFile, got, 0644)
        return
    }

    want, err := os.ReadFile(goldenFile)
    if err != nil {
        t.Fatalf("failed to read golden file: %v", err)
    }

    if !bytes.Equal(got, want) {
        t.Errorf("output mismatch:\ngot:\n%s\nwant:\n%s", got, want)
    }
}

var update = flag.Bool("update", false, "update golden files")
```

## モック

### インターフェースによるモック

```go
// repository.go
type UserRepository interface {
    Find(ctx context.Context, id int64) (*User, error)
    Save(ctx context.Context, user *User) error
}

// service_test.go
type mockUserRepository struct {
    findFunc func(ctx context.Context, id int64) (*User, error)
    saveFunc func(ctx context.Context, user *User) error
}

func (m *mockUserRepository) Find(ctx context.Context, id int64) (*User, error) {
    if m.findFunc != nil {
        return m.findFunc(ctx, id)
    }
    return nil, errors.New("not implemented")
}

func (m *mockUserRepository) Save(ctx context.Context, user *User) error {
    if m.saveFunc != nil {
        return m.saveFunc(ctx, user)
    }
    return errors.New("not implemented")
}

func TestUserService_Create(t *testing.T) {
    repo := &mockUserRepository{
        saveFunc: func(ctx context.Context, user *User) error {
            user.ID = 1 // 保存時にIDを設定
            return nil
        },
    }

    service := NewUserService(repo)
    user, err := service.Create(context.Background(), "Alice", "alice@example.com")

    if err != nil {
        t.Fatalf("unexpected error: %v", err)
    }
    if user.ID != 1 {
        t.Errorf("ID: got %d, want 1", user.ID)
    }
}
```

### go generate + moq

```go
//go:generate moq -out repository_mock.go . UserRepository

type UserRepository interface {
    Find(ctx context.Context, id int64) (*User, error)
}

// 生成されたモックを使用
func TestWithGeneratedMock(t *testing.T) {
    mock := &UserRepositoryMock{
        FindFunc: func(ctx context.Context, id int64) (*User, error) {
            return &User{ID: id, Name: "Test"}, nil
        },
    }

    // mockを使用...

    // 呼び出し検証
    if len(mock.FindCalls()) != 1 {
        t.Error("Find was not called")
    }
}
```

## 並行テスト

### t.Parallel

```go
func TestConcurrent(t *testing.T) {
    tests := []struct {
        name  string
        input int
        want  int
    }{
        {"case1", 1, 2},
        {"case2", 2, 4},
        {"case3", 3, 6},
    }

    for _, tt := range tests {
        tt := tt // Go 1.22以前ではキャプチャが必要
        t.Run(tt.name, func(t *testing.T) {
            t.Parallel() // 並行実行を有効化
            got := Double(tt.input)
            if got != tt.want {
                t.Errorf("got %d, want %d", got, tt.want)
            }
        })
    }
}
```

### レースディテクタ

```bash
go test -race ./...
```

## 統合テスト

### ビルドタグ

```go
//go:build integration

package user_test

import (
    "testing"
    "database/sql"
)

func TestUserRepository_Integration(t *testing.T) {
    db, err := sql.Open("postgres", os.Getenv("TEST_DATABASE_URL"))
    if err != nil {
        t.Fatalf("failed to connect: %v", err)
    }
    defer db.Close()

    // 統合テスト...
}
```

```bash
# 統合テストを実行
go test -tags=integration ./...
```

### TestMain

```go
func TestMain(m *testing.M) {
    // セットアップ
    pool, err := setupTestContainer()
    if err != nil {
        log.Fatalf("setup: %v", err)
    }

    code := m.Run()

    // クリーンアップ
    pool.Purge()

    os.Exit(code)
}
```

## ベンチマーク

### 基本的なベンチマーク

```go
func BenchmarkProcess(b *testing.B) {
    data := generateTestData(1000)

    b.ResetTimer() // セットアップ時間を除外

    for i := 0; i < b.N; i++ {
        Process(data)
    }
}
```

### メモリアロケーション

```go
func BenchmarkProcess(b *testing.B) {
    data := generateTestData(1000)

    b.ReportAllocs() // アロケーション数を報告
    b.ResetTimer()

    for i := 0; i < b.N; i++ {
        Process(data)
    }
}
```

```bash
# ベンチマーク実行
go test -bench=. -benchmem ./...
```

### サブベンチマーク

```go
func BenchmarkProcess(b *testing.B) {
    sizes := []int{10, 100, 1000, 10000}

    for _, size := range sizes {
        b.Run(fmt.Sprintf("size=%d", size), func(b *testing.B) {
            data := generateTestData(size)
            b.ResetTimer()

            for i := 0; i < b.N; i++ {
                Process(data)
            }
        })
    }
}
```
