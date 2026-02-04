# Go標準ライブラリリファレンス

## 目次

1. [context](#context)
2. [errors](#errors)
3. [fmt](#fmt)
4. [io](#io)
5. [net/http](#nethttp)
6. [encoding/json](#encodingjson)
7. [sync](#sync)
8. [time](#time)
9. [log/slog](#logslog)

## context

### 基本的な使い方

```go
import "context"

// 背景コンテキスト（ルート）
ctx := context.Background()

// TODO用（後でコンテキストを追加予定の場合）
ctx := context.TODO()

// タイムアウト付きコンテキスト
ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
defer cancel()

// デッドライン付きコンテキスト
deadline := time.Now().Add(10 * time.Second)
ctx, cancel := context.WithDeadline(ctx, deadline)
defer cancel()

// キャンセル可能なコンテキスト
ctx, cancel := context.WithCancel(ctx)
defer cancel()

// 値を持つコンテキスト
type keyType string
const userKey keyType = "user"
ctx = context.WithValue(ctx, userKey, user)

// 値の取得
if user, ok := ctx.Value(userKey).(*User); ok {
    // userを使用
}
```

### コンテキストのキャンセル確認

```go
select {
case <-ctx.Done():
    return ctx.Err() // context.Canceled または context.DeadlineExceeded
default:
    // 処理を続行
}
```

## errors

### 基本的な使い方

```go
import "errors"

// エラー作成
err := errors.New("something went wrong")

// センチネルエラー
var ErrNotFound = errors.New("not found")

// エラーのラップ（fmt.Errorf使用）
err := fmt.Errorf("failed to process: %w", originalErr)

// エラーの判定
if errors.Is(err, ErrNotFound) {
    // ErrNotFoundまたはそれをラップしたエラー
}

// エラーの型変換
var pathErr *os.PathError
if errors.As(err, &pathErr) {
    // pathErrとして使用可能
}

// 複数エラーの結合（Go 1.20+）
err := errors.Join(err1, err2, err3)
```

## fmt

### 出力

```go
import "fmt"

// 標準出力
fmt.Print("hello")      // 改行なし
fmt.Println("hello")    // 改行あり
fmt.Printf("name: %s, age: %d\n", name, age)

// 文字列生成
s := fmt.Sprint("hello")
s := fmt.Sprintf("name: %s", name)

// io.Writerへ出力
fmt.Fprint(w, "hello")
fmt.Fprintf(w, "name: %s", name)
```

### フォーマット指定子

```go
%v    // デフォルト形式
%+v   // 構造体のフィールド名付き
%#v   // Go構文での表現
%T    // 型名

%s    // 文字列
%q    // クォート付き文字列
%d    // 10進数
%x    // 16進数（小文字）
%X    // 16進数（大文字）
%f    // 浮動小数点
%e    // 指数表記
%t    // bool
%p    // ポインタ

%w    // エラーのラップ（errors.Isで検出可能）
```

## io

### 主要なインターフェース

```go
import "io"

type Reader interface {
    Read(p []byte) (n int, err error)
}

type Writer interface {
    Write(p []byte) (n int, err error)
}

type Closer interface {
    Close() error
}

type ReadWriter interface {
    Reader
    Writer
}

type ReadCloser interface {
    Reader
    Closer
}
```

### ユーティリティ関数

```go
// 全て読み込み
data, err := io.ReadAll(r)

// コピー
n, err := io.Copy(dst, src)

// 制限付きReader
limited := io.LimitReader(r, 1024) // 最大1024バイト

// 複数Readerの結合
multi := io.MultiReader(r1, r2, r3)

// 複数Writerへ同時書き込み
multi := io.MultiWriter(w1, w2, w3)

// 読み込みと同時にコピー
tee := io.TeeReader(r, w)

// EOF
if err == io.EOF {
    // ファイル終端
}
```

## net/http

### HTTPクライアント

```go
import "net/http"

// シンプルなGET
resp, err := http.Get("https://example.com")
if err != nil {
    return err
}
defer resp.Body.Close()

body, err := io.ReadAll(resp.Body)

// カスタムクライアント
client := &http.Client{
    Timeout: 10 * time.Second,
    Transport: &http.Transport{
        MaxIdleConns:        100,
        MaxIdleConnsPerHost: 10,
        IdleConnTimeout:     90 * time.Second,
    },
}

// カスタムリクエスト
req, err := http.NewRequestWithContext(ctx, "POST", url, body)
req.Header.Set("Content-Type", "application/json")
req.Header.Set("Authorization", "Bearer "+token)

resp, err := client.Do(req)
```

### HTTPサーバー

```go
// ハンドラ関数
http.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) {
    fmt.Fprintf(w, "Hello, %s!", r.URL.Query().Get("name"))
})

// Handlerインターフェース
type Handler interface {
    ServeHTTP(ResponseWriter, *Request)
}

// サーバー起動
server := &http.Server{
    Addr:         ":8080",
    Handler:      mux,
    ReadTimeout:  10 * time.Second,
    WriteTimeout: 10 * time.Second,
}

err := server.ListenAndServe()

// グレースフルシャットダウン
ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
defer cancel()
server.Shutdown(ctx)
```

## encoding/json

### エンコード/デコード

```go
import "encoding/json"

// 構造体 → JSON
type User struct {
    ID    int64  `json:"id"`
    Name  string `json:"name"`
    Email string `json:"email,omitempty"`
}

data, err := json.Marshal(user)
data, err := json.MarshalIndent(user, "", "  ") // 整形

// JSON → 構造体
var user User
err := json.Unmarshal(data, &user)

// io.Reader/Writerを使用
err := json.NewDecoder(r).Decode(&user)
err := json.NewEncoder(w).Encode(user)
```

### 構造体タグ

```go
type Config struct {
    Name     string `json:"name"`           // フィールド名変更
    Value    int    `json:"value,omitempty"` // ゼロ値は省略
    Internal string `json:"-"`              // JSONから除外
    Raw      json.RawMessage `json:"raw"`   // 遅延デコード
}
```

## sync

### Mutex

```go
import "sync"

var mu sync.Mutex

mu.Lock()
defer mu.Unlock()
// クリティカルセクション

// 読み書きロック
var rwmu sync.RWMutex
rwmu.RLock()    // 読み取りロック（複数可）
rwmu.RUnlock()
rwmu.Lock()     // 書き込みロック（排他）
rwmu.Unlock()
```

### WaitGroup

```go
var wg sync.WaitGroup

for i := 0; i < 10; i++ {
    wg.Add(1)
    go func() {
        defer wg.Done()
        // 処理
    }()
}

wg.Wait() // 全ゴルーチンの完了を待機
```

### Once

```go
var once sync.Once
var instance *Config

func GetConfig() *Config {
    once.Do(func() {
        instance = loadConfig()
    })
    return instance
}
```

### Map

```go
var m sync.Map

m.Store("key", "value")

if value, ok := m.Load("key"); ok {
    // valueを使用
}

m.Delete("key")

m.Range(func(key, value any) bool {
    // 全エントリを走査
    return true // falseで中断
})
```

## time

### 時刻操作

```go
import "time"

// 現在時刻
now := time.Now()

// 時刻の作成
t := time.Date(2024, time.January, 1, 12, 0, 0, 0, time.UTC)

// Duration
d := 5 * time.Second
d := time.Hour + 30*time.Minute

// 加算/減算
future := now.Add(24 * time.Hour)
past := now.Add(-1 * time.Hour)
diff := t1.Sub(t2) // Duration

// 比較
if t1.Before(t2) {}
if t1.After(t2) {}
if t1.Equal(t2) {}
```

### フォーマット

```go
// フォーマット（参照時刻: 2006-01-02 15:04:05）
s := t.Format("2006-01-02 15:04:05")
s := t.Format(time.RFC3339)

// パース
t, err := time.Parse("2006-01-02", "2024-01-15")
t, err := time.Parse(time.RFC3339, "2024-01-15T12:00:00Z")
```

### タイマー

```go
// スリープ
time.Sleep(1 * time.Second)

// タイマー
timer := time.NewTimer(5 * time.Second)
<-timer.C // 5秒後に受信

// ティッカー
ticker := time.NewTicker(1 * time.Second)
defer ticker.Stop()
for range ticker.C {
    // 1秒ごとに実行
}

// タイムアウト
select {
case result := <-ch:
    // 結果を処理
case <-time.After(5 * time.Second):
    // タイムアウト
}
```

## log/slog

### 基本的な使い方（Go 1.21+）

```go
import "log/slog"

// デフォルトロガー
slog.Info("message", "key", "value")
slog.Debug("debug message")
slog.Warn("warning", "count", 42)
slog.Error("error occurred", "err", err)

// カスタムロガー
logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
    Level: slog.LevelDebug,
}))

// グループ化
logger.Info("request",
    slog.Group("user",
        slog.Int64("id", user.ID),
        slog.String("name", user.Name),
    ),
)

// コンテキスト付き
logger.InfoContext(ctx, "processing", "id", id)

// 属性付きロガー
logger = logger.With("service", "api", "version", "1.0")
```
