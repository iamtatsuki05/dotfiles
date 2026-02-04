# よく使うパターン集

## 目次

1. [デザインパターン](#デザインパターン)
2. [関数型パターン](#関数型パターン)
3. [非同期パターン](#非同期パターン)
4. [DI（依存性注入）](#di依存性注入)
5. [設定管理](#設定管理)
6. [バリデーション](#バリデーション)

## デザインパターン

### ファクトリ

```typescript
interface Logger {
  log(message: string): void;
}

class ConsoleLogger implements Logger {
  log(message: string): void {
    console.log(message);
  }
}

class FileLogger implements Logger {
  constructor(private filePath: string) {}
  log(message: string): void {
    // ファイルに書き込み
  }
}

type LoggerType = 'console' | 'file';

class LoggerFactory {
  private static readonly loggers = new Map<LoggerType, () => Logger>([
    ['console', () => new ConsoleLogger()],
    ['file', () => new FileLogger('/var/log/app.log')],
  ]);

  static create(type: LoggerType): Logger {
    const factory = this.loggers.get(type);
    if (!factory) {
      throw new Error(`Unknown logger type: ${type}`);
    }
    return factory();
  }
}

// 使用
const logger = LoggerFactory.create('console');
```

### ビルダー

```typescript
interface RequestConfig {
  url: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  headers: Record<string, string>;
  body?: unknown;
  timeout: number;
}

class RequestBuilder {
  private config: Partial<RequestConfig> = {
    method: 'GET',
    headers: {},
    timeout: 5000,
  };

  url(url: string): this {
    this.config.url = url;
    return this;
  }

  method(method: RequestConfig['method']): this {
    this.config.method = method;
    return this;
  }

  header(key: string, value: string): this {
    this.config.headers = { ...this.config.headers, [key]: value };
    return this;
  }

  body(body: unknown): this {
    this.config.body = body;
    return this;
  }

  timeout(ms: number): this {
    this.config.timeout = ms;
    return this;
  }

  build(): RequestConfig {
    if (!this.config.url) {
      throw new Error('URL is required');
    }
    return this.config as RequestConfig;
  }
}

// 使用
const request = new RequestBuilder()
  .url('https://api.example.com/users')
  .method('POST')
  .header('Content-Type', 'application/json')
  .body({ name: 'Test' })
  .build();
```

### ストラテジー

```typescript
interface PricingStrategy {
  calculate(basePrice: number): number;
}

class RegularPricing implements PricingStrategy {
  calculate(basePrice: number): number {
    return basePrice;
  }
}

class DiscountPricing implements PricingStrategy {
  constructor(private discountRate: number) {}

  calculate(basePrice: number): number {
    return basePrice * (1 - this.discountRate);
  }
}

class PremiumPricing implements PricingStrategy {
  calculate(basePrice: number): number {
    return basePrice * 1.2;  // 20% プレミアム
  }
}

class PriceCalculator {
  constructor(private strategy: PricingStrategy) {}

  setStrategy(strategy: PricingStrategy): void {
    this.strategy = strategy;
  }

  calculate(basePrice: number): number {
    return this.strategy.calculate(basePrice);
  }
}

// 使用
const calculator = new PriceCalculator(new RegularPricing());
console.log(calculator.calculate(100));  // 100

calculator.setStrategy(new DiscountPricing(0.2));
console.log(calculator.calculate(100));  // 80
```

### シングルトン

```typescript
class Database {
  private static instance: Database | null = null;

  private constructor(private connectionString: string) {}

  static getInstance(): Database {
    if (!Database.instance) {
      Database.instance = new Database(
        process.env.DATABASE_URL ?? 'localhost:5432',
      );
    }
    return Database.instance;
  }

  query(sql: string): Promise<unknown[]> {
    // クエリ実行
    return Promise.resolve([]);
  }
}

// 使用
const db1 = Database.getInstance();
const db2 = Database.getInstance();
console.log(db1 === db2);  // true
```

## 関数型パターン

### Result型

```typescript
type Result<T, E = Error> =
  | { success: true; data: T }
  | { success: false; error: E };

function ok<T>(data: T): Result<T, never> {
  return { success: true, data };
}

function err<E>(error: E): Result<never, E> {
  return { success: false, error };
}

// 使用
function divide(a: number, b: number): Result<number, string> {
  if (b === 0) {
    return err('Division by zero');
  }
  return ok(a / b);
}

const result = divide(10, 2);
if (result.success) {
  console.log(result.data);  // 5
} else {
  console.error(result.error);
}
```

### Option型

```typescript
type Option<T> = { some: true; value: T } | { some: false };

function some<T>(value: T): Option<T> {
  return { some: true, value };
}

function none<T>(): Option<T> {
  return { some: false };
}

function map<T, U>(option: Option<T>, fn: (value: T) => U): Option<U> {
  if (option.some) {
    return some(fn(option.value));
  }
  return none();
}

function getOrElse<T>(option: Option<T>, defaultValue: T): T {
  if (option.some) {
    return option.value;
  }
  return defaultValue;
}

// 使用
function findUser(id: string): Option<User> {
  const user = users.get(id);
  return user ? some(user) : none();
}
```

### パイプライン

```typescript
function pipe<A, B>(a: A, fn: (a: A) => B): B;
function pipe<A, B, C>(a: A, fn1: (a: A) => B, fn2: (b: B) => C): C;
function pipe<A, B, C, D>(
  a: A,
  fn1: (a: A) => B,
  fn2: (b: B) => C,
  fn3: (c: C) => D,
): D;
function pipe(initial: unknown, ...fns: Array<(x: unknown) => unknown>): unknown {
  return fns.reduce((acc, fn) => fn(acc), initial);
}

// 使用
const result = pipe(
  '  hello world  ',
  (s: string) => s.trim(),
  (s: string) => s.toUpperCase(),
  (s: string) => s.split(' '),
);
// ['HELLO', 'WORLD']
```

## 非同期パターン

### 並行実行制限

```typescript
async function asyncPool<T, R>(
  poolLimit: number,
  items: T[],
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = [];
  const executing: Promise<void>[] = [];

  for (const item of items) {
    const promise = fn(item).then((result) => {
      results.push(result);
    });

    executing.push(promise);

    if (executing.length >= poolLimit) {
      await Promise.race(executing);
      executing.splice(
        executing.findIndex((p) => p === promise),
        1,
      );
    }
  }

  await Promise.all(executing);
  return results;
}

// 使用
const urls = ['url1', 'url2', 'url3', 'url4', 'url5'];
const results = await asyncPool(2, urls, fetchData);
```

### リトライ

```typescript
interface RetryOptions {
  maxAttempts: number;
  delay: number;
  backoff?: 'linear' | 'exponential';
}

async function retry<T>(
  fn: () => Promise<T>,
  options: RetryOptions,
): Promise<T> {
  const { maxAttempts, delay, backoff = 'linear' } = options;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (error) {
      if (attempt === maxAttempts) {
        throw error;
      }

      const waitTime =
        backoff === 'exponential' ? delay * Math.pow(2, attempt - 1) : delay;

      await new Promise((resolve) => setTimeout(resolve, waitTime));
    }
  }

  throw new Error('Unreachable');
}

// 使用
const data = await retry(() => fetchData('/api/data'), {
  maxAttempts: 3,
  delay: 1000,
  backoff: 'exponential',
});
```

### デバウンス/スロットル

```typescript
function debounce<T extends (...args: unknown[]) => void>(
  fn: T,
  wait: number,
): (...args: Parameters<T>) => void {
  let timeoutId: NodeJS.Timeout | null = null;

  return (...args: Parameters<T>) => {
    if (timeoutId) {
      clearTimeout(timeoutId);
    }
    timeoutId = setTimeout(() => fn(...args), wait);
  };
}

function throttle<T extends (...args: unknown[]) => void>(
  fn: T,
  limit: number,
): (...args: Parameters<T>) => void {
  let lastCall = 0;

  return (...args: Parameters<T>) => {
    const now = Date.now();
    if (now - lastCall >= limit) {
      lastCall = now;
      fn(...args);
    }
  };
}
```

## DI（依存性注入）

### tsyringe

```typescript
import 'reflect-metadata';
import { container, injectable, inject } from 'tsyringe';

interface ILogger {
  log(message: string): void;
}

interface IUserRepository {
  findById(id: string): Promise<User | null>;
}

@injectable()
class ConsoleLogger implements ILogger {
  log(message: string): void {
    console.log(message);
  }
}

@injectable()
class UserService {
  constructor(
    @inject('Logger') private logger: ILogger,
    @inject('UserRepository') private repository: IUserRepository,
  ) {}

  async getUser(id: string): Promise<User | null> {
    this.logger.log(`Getting user: ${id}`);
    return this.repository.findById(id);
  }
}

// 登録
container.register<ILogger>('Logger', { useClass: ConsoleLogger });
container.register<IUserRepository>('UserRepository', {
  useClass: UserRepository,
});

// 解決
const userService = container.resolve(UserService);
```

### 手動DI

```typescript
// インターフェース
interface Dependencies {
  logger: Logger;
  config: Config;
  repository: UserRepository;
}

// ファクトリ関数
function createUserService(deps: Dependencies): UserService {
  return new UserService(deps.logger, deps.config, deps.repository);
}

// コンテナ
function createContainer(): Dependencies {
  const config = new Config();
  const logger = new ConsoleLogger(config);
  const repository = new UserRepository(config);

  return { logger, config, repository };
}

// 使用
const container = createContainer();
const userService = createUserService(container);
```

## 設定管理

### 環境変数

```typescript
import { z } from 'zod';

const envSchema = z.object({
  NODE_ENV: z.enum(['development', 'production', 'test']).default('development'),
  PORT: z.string().transform(Number).default('3000'),
  DATABASE_URL: z.string().url(),
  API_KEY: z.string().min(1),
  DEBUG: z.string().transform((v) => v === 'true').default('false'),
});

type Env = z.infer<typeof envSchema>;

function loadEnv(): Env {
  const result = envSchema.safeParse(process.env);
  if (!result.success) {
    console.error('Invalid environment variables:');
    console.error(result.error.format());
    process.exit(1);
  }
  return result.data;
}

export const env = loadEnv();
```

### 設定クラス

```typescript
class Config {
  private static instance: Config | null = null;

  readonly database: {
    url: string;
    maxConnections: number;
  };

  readonly server: {
    port: number;
    host: string;
  };

  private constructor() {
    this.database = {
      url: process.env.DATABASE_URL ?? 'postgres://localhost:5432/app',
      maxConnections: Number(process.env.DB_MAX_CONNECTIONS) || 10,
    };

    this.server = {
      port: Number(process.env.PORT) || 3000,
      host: process.env.HOST ?? '0.0.0.0',
    };
  }

  static getInstance(): Config {
    if (!Config.instance) {
      Config.instance = new Config();
    }
    return Config.instance;
  }
}

export const config = Config.getInstance();
```

## バリデーション

### Zod

```typescript
import { z } from 'zod';

// スキーマ定義
const userSchema = z.object({
  id: z.string().uuid(),
  name: z.string().min(1).max(100),
  email: z.string().email(),
  age: z.number().int().positive().optional(),
  role: z.enum(['admin', 'user', 'guest']),
  tags: z.array(z.string()).default([]),
  metadata: z.record(z.string(), z.unknown()).optional(),
});

// 型の推論
type User = z.infer<typeof userSchema>;

// バリデーション
function validateUser(data: unknown): User {
  return userSchema.parse(data);
}

// 安全なバリデーション
function safeValidateUser(data: unknown): { success: true; data: User } | { success: false; error: z.ZodError } {
  const result = userSchema.safeParse(data);
  return result;
}

// 部分スキーマ
const updateUserSchema = userSchema.partial().omit({ id: true });
type UpdateUser = z.infer<typeof updateUserSchema>;

// 変換
const userWithDefaults = userSchema.transform((user) => ({
  ...user,
  createdAt: new Date(),
}));
```

### カスタムバリデーション

```typescript
import { z } from 'zod';

// カスタムバリデーション
const passwordSchema = z
  .string()
  .min(8)
  .refine((val) => /[A-Z]/.test(val), {
    message: 'Must contain at least one uppercase letter',
  })
  .refine((val) => /[a-z]/.test(val), {
    message: 'Must contain at least one lowercase letter',
  })
  .refine((val) => /[0-9]/.test(val), {
    message: 'Must contain at least one number',
  });

// 相互依存バリデーション
const dateRangeSchema = z
  .object({
    startDate: z.date(),
    endDate: z.date(),
  })
  .refine((data) => data.endDate > data.startDate, {
    message: 'End date must be after start date',
    path: ['endDate'],
  });
```
