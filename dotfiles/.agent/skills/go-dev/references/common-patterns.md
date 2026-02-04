# よく使うパターン集

## 目次

1. [デザインパターン](#デザインパターン)
2. [並行処理パターン](#並行処理パターン)
3. [HTTPパターン](#httpパターン)
4. [データベースパターン](#データベースパターン)
5. [設定管理](#設定管理)

## デザインパターン

### ファクトリ

```go
type Storage interface {
    Save(ctx context.Context, key string, data []byte) error
    Load(ctx context.Context, key string) ([]byte, error)
}

type StorageType string

const (
    StorageTypeFile  StorageType = "file"
    StorageTypeS3    StorageType = "s3"
    StorageTypeRedis StorageType = "redis"
)

func NewStorage(typ StorageType, cfg Config) (Storage, error) {
    switch typ {
    case StorageTypeFile:
        return NewFileStorage(cfg.BasePath), nil
    case StorageTypeS3:
        return NewS3Storage(cfg.Bucket, cfg.Region)
    case StorageTypeRedis:
        return NewRedisStorage(cfg.RedisURL)
    default:
        return nil, fmt.Errorf("unknown storage type: %s", typ)
    }
}
```

### オプションパターン（Functional Options）

```go
type Server struct {
    addr         string
    readTimeout  time.Duration
    writeTimeout time.Duration
    logger       *slog.Logger
}

type ServerOption func(*Server)

func WithReadTimeout(d time.Duration) ServerOption {
    return func(s *Server) {
        s.readTimeout = d
    }
}

func WithWriteTimeout(d time.Duration) ServerOption {
    return func(s *Server) {
        s.writeTimeout = d
    }
}

func WithLogger(logger *slog.Logger) ServerOption {
    return func(s *Server) {
        s.logger = logger
    }
}

func NewServer(addr string, opts ...ServerOption) *Server {
    s := &Server{
        addr:         addr,
        readTimeout:  30 * time.Second,
        writeTimeout: 30 * time.Second,
        logger:       slog.Default(),
    }
    for _, opt := range opts {
        opt(s)
    }
    return s
}

// 使用例
server := NewServer(":8080",
    WithReadTimeout(10*time.Second),
    WithLogger(customLogger),
)
```

### ビルダー

```go
type Request struct {
    method  string
    url     string
    headers map[string]string
    body    io.Reader
}

type RequestBuilder struct {
    request Request
}

func NewRequestBuilder(method, url string) *RequestBuilder {
    return &RequestBuilder{
        request: Request{
            method:  method,
            url:     url,
            headers: make(map[string]string),
        },
    }
}

func (b *RequestBuilder) Header(key, value string) *RequestBuilder {
    b.request.headers[key] = value
    return b
}

func (b *RequestBuilder) Body(body io.Reader) *RequestBuilder {
    b.request.body = body
    return b
}

func (b *RequestBuilder) Build() (*http.Request, error) {
    req, err := http.NewRequest(b.request.method, b.request.url, b.request.body)
    if err != nil {
        return nil, err
    }
    for k, v := range b.request.headers {
        req.Header.Set(k, v)
    }
    return req, nil
}

// 使用例
req, err := NewRequestBuilder("POST", "https://api.example.com/users").
    Header("Content-Type", "application/json").
    Header("Authorization", "Bearer token").
    Body(strings.NewReader(`{"name":"Alice"}`)).
    Build()
```

### シングルトン（sync.Once）

```go
var (
    instance *Config
    once     sync.Once
)

func GetConfig() *Config {
    once.Do(func() {
        instance = loadConfig()
    })
    return instance
}
```

## 並行処理パターン

### errgroup

```go
import "golang.org/x/sync/errgroup"

func FetchAll(ctx context.Context, urls []string) ([]Response, error) {
    g, ctx := errgroup.WithContext(ctx)
    responses := make([]Response, len(urls))

    for i, url := range urls {
        i, url := i, url // Go 1.22以前ではキャプチャが必要
        g.Go(func() error {
            resp, err := fetch(ctx, url)
            if err != nil {
                return fmt.Errorf("fetch %s: %w", url, err)
            }
            responses[i] = resp
            return nil
        })
    }

    if err := g.Wait(); err != nil {
        return nil, err
    }
    return responses, nil
}
```

### セマフォによる並行数制限

```go
import "golang.org/x/sync/semaphore"

func ProcessItems(ctx context.Context, items []Item, maxConcurrency int64) error {
    sem := semaphore.NewWeighted(maxConcurrency)
    g, ctx := errgroup.WithContext(ctx)

    for _, item := range items {
        item := item
        if err := sem.Acquire(ctx, 1); err != nil {
            return err
        }

        g.Go(func() error {
            defer sem.Release(1)
            return process(ctx, item)
        })
    }

    return g.Wait()
}
```

### ワーカープール

```go
func WorkerPool(ctx context.Context, jobs <-chan Job, numWorkers int) <-chan Result {
    results := make(chan Result, numWorkers)
    var wg sync.WaitGroup

    for i := 0; i < numWorkers; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for job := range jobs {
                select {
                case <-ctx.Done():
                    return
                case results <- process(job):
                }
            }
        }()
    }

    go func() {
        wg.Wait()
        close(results)
    }()

    return results
}
```

### Fan-out/Fan-in

```go
func FanOut(ctx context.Context, input <-chan int, numWorkers int) []<-chan int {
    outputs := make([]<-chan int, numWorkers)
    for i := 0; i < numWorkers; i++ {
        outputs[i] = worker(ctx, input)
    }
    return outputs
}

func FanIn(ctx context.Context, channels ...<-chan int) <-chan int {
    var wg sync.WaitGroup
    merged := make(chan int)

    output := func(c <-chan int) {
        defer wg.Done()
        for n := range c {
            select {
            case merged <- n:
            case <-ctx.Done():
                return
            }
        }
    }

    wg.Add(len(channels))
    for _, c := range channels {
        go output(c)
    }

    go func() {
        wg.Wait()
        close(merged)
    }()

    return merged
}
```

### リトライ

```go
type RetryConfig struct {
    MaxAttempts int
    InitialWait time.Duration
    MaxWait     time.Duration
}

func WithRetry[T any](ctx context.Context, cfg RetryConfig, fn func() (T, error)) (T, error) {
    var result T
    var err error
    wait := cfg.InitialWait

    for attempt := 0; attempt < cfg.MaxAttempts; attempt++ {
        result, err = fn()
        if err == nil {
            return result, nil
        }

        if attempt == cfg.MaxAttempts-1 {
            break
        }

        select {
        case <-ctx.Done():
            return result, ctx.Err()
        case <-time.After(wait):
        }

        wait = min(wait*2, cfg.MaxWait)
    }

    return result, fmt.Errorf("max retries exceeded: %w", err)
}
```

## HTTPパターン

### ミドルウェア

```go
type Middleware func(http.Handler) http.Handler

func Chain(middlewares ...Middleware) Middleware {
    return func(next http.Handler) http.Handler {
        for i := len(middlewares) - 1; i >= 0; i-- {
            next = middlewares[i](next)
        }
        return next
    }
}

func Logging(logger *slog.Logger) Middleware {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            start := time.Now()
            next.ServeHTTP(w, r)
            logger.Info("request",
                "method", r.Method,
                "path", r.URL.Path,
                "duration", time.Since(start),
            )
        })
    }
}

func Recover() Middleware {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            defer func() {
                if err := recover(); err != nil {
                    http.Error(w, "Internal Server Error", http.StatusInternalServerError)
                }
            }()
            next.ServeHTTP(w, r)
        })
    }
}
```

### JSONレスポンス

```go
func JSON(w http.ResponseWriter, status int, data any) {
    w.Header().Set("Content-Type", "application/json")
    w.WriteHeader(status)
    json.NewEncoder(w).Encode(data)
}

func Error(w http.ResponseWriter, status int, message string) {
    JSON(w, status, map[string]string{"error": message})
}
```

### リクエストバリデーション

```go
func DecodeAndValidate[T any](r *http.Request) (T, error) {
    var v T
    if err := json.NewDecoder(r.Body).Decode(&v); err != nil {
        return v, fmt.Errorf("decode: %w", err)
    }

    // バリデーションインターフェースをチェック
    if validator, ok := any(&v).(interface{ Validate() error }); ok {
        if err := validator.Validate(); err != nil {
            return v, fmt.Errorf("validate: %w", err)
        }
    }

    return v, nil
}
```

## データベースパターン

### リポジトリ

```go
type UserRepository interface {
    Find(ctx context.Context, id int64) (*User, error)
    FindByEmail(ctx context.Context, email string) (*User, error)
    Save(ctx context.Context, user *User) error
    Delete(ctx context.Context, id int64) error
}

type postgresUserRepository struct {
    db *sql.DB
}

func NewPostgresUserRepository(db *sql.DB) UserRepository {
    return &postgresUserRepository{db: db}
}

func (r *postgresUserRepository) Find(ctx context.Context, id int64) (*User, error) {
    var u User
    err := r.db.QueryRowContext(ctx,
        "SELECT id, name, email, created_at FROM users WHERE id = $1",
        id,
    ).Scan(&u.ID, &u.Name, &u.Email, &u.CreatedAt)

    if errors.Is(err, sql.ErrNoRows) {
        return nil, ErrUserNotFound
    }
    if err != nil {
        return nil, fmt.Errorf("query: %w", err)
    }
    return &u, nil
}
```

### トランザクション

```go
type TxFunc func(ctx context.Context, tx *sql.Tx) error

func WithTransaction(ctx context.Context, db *sql.DB, fn TxFunc) error {
    tx, err := db.BeginTx(ctx, nil)
    if err != nil {
        return fmt.Errorf("begin tx: %w", err)
    }

    if err := fn(ctx, tx); err != nil {
        if rbErr := tx.Rollback(); rbErr != nil {
            return fmt.Errorf("rollback: %v (original: %w)", rbErr, err)
        }
        return err
    }

    if err := tx.Commit(); err != nil {
        return fmt.Errorf("commit: %w", err)
    }
    return nil
}

// 使用例
err := WithTransaction(ctx, db, func(ctx context.Context, tx *sql.Tx) error {
    if _, err := tx.ExecContext(ctx, "UPDATE accounts SET balance = balance - $1 WHERE id = $2", amount, fromID); err != nil {
        return err
    }
    if _, err := tx.ExecContext(ctx, "UPDATE accounts SET balance = balance + $1 WHERE id = $2", amount, toID); err != nil {
        return err
    }
    return nil
})
```

## 設定管理

### 環境変数から読み込み

```go
type Config struct {
    ServerAddr   string        `env:"SERVER_ADDR" default:":8080"`
    DatabaseURL  string        `env:"DATABASE_URL,required"`
    ReadTimeout  time.Duration `env:"READ_TIMEOUT" default:"30s"`
    WriteTimeout time.Duration `env:"WRITE_TIMEOUT" default:"30s"`
    Debug        bool          `env:"DEBUG" default:"false"`
}

func LoadConfig() (*Config, error) {
    cfg := &Config{}

    if addr := os.Getenv("SERVER_ADDR"); addr != "" {
        cfg.ServerAddr = addr
    } else {
        cfg.ServerAddr = ":8080"
    }

    dbURL := os.Getenv("DATABASE_URL")
    if dbURL == "" {
        return nil, errors.New("DATABASE_URL is required")
    }
    cfg.DatabaseURL = dbURL

    if timeout := os.Getenv("READ_TIMEOUT"); timeout != "" {
        d, err := time.ParseDuration(timeout)
        if err != nil {
            return nil, fmt.Errorf("parse READ_TIMEOUT: %w", err)
        }
        cfg.ReadTimeout = d
    } else {
        cfg.ReadTimeout = 30 * time.Second
    }

    cfg.Debug = os.Getenv("DEBUG") == "true"

    return cfg, nil
}
```

### envconfig ライブラリ

```go
import "github.com/kelseyhightower/envconfig"

type Config struct {
    ServerAddr   string        `envconfig:"SERVER_ADDR" default:":8080"`
    DatabaseURL  string        `envconfig:"DATABASE_URL" required:"true"`
    ReadTimeout  time.Duration `envconfig:"READ_TIMEOUT" default:"30s"`
    Debug        bool          `envconfig:"DEBUG" default:"false"`
}

func LoadConfig() (*Config, error) {
    var cfg Config
    if err := envconfig.Process("", &cfg); err != nil {
        return nil, fmt.Errorf("load config: %w", err)
    }
    return &cfg, nil
}
```

### 遅延初期化

```go
var (
    config     *Config
    configOnce sync.Once
    configErr  error
)

func GetConfig() (*Config, error) {
    configOnce.Do(func() {
        config, configErr = LoadConfig()
    })
    return config, configErr
}
```
