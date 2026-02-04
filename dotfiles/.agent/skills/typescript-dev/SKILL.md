---
name: typescript-dev
description: TypeScript開発のための汎用スキル。コード実装、リファクタリング、テスト作成、デバッグ、コードレビューを支援。TypeScriptファイル(.ts/.tsx)の作成・編集、Jest/Vitestによるテスト、型定義追加、コード品質改善、エラー解決、ベストプラクティス適用時に使用。
---

# TypeScript開発スキル

TypeScriptコードの実装、テスト、デバッグ、リファクタリングを効率的に行うためのガイド。

## 実装前の必須確認

**tsconfig.json/package.jsonを必ず確認する。** プロジェクトの設定に従う。

確認項目:
- `tsconfig.json`: target, module, strict, paths, baseUrl
- `package.json`: type（"module"/"commonjs"）, scripts
- `.eslintrc`/`eslint.config.js`: ESLint設定
- `.prettierrc`: フォーマット設定
- `biome.json`: Biome使用時の設定

## 型定義

### 基本的な型

```typescript
// プリミティブ
const name: string = 'example';
const count: number = 42;
const isActive: boolean = true;

// 配列
const items: string[] = [];
const numbers: Array<number> = [];

// タプル
const point: [number, number] = [0, 0];

// Union型
type Status = 'pending' | 'success' | 'error';
let value: string | null = null;

// オブジェクト型
interface User {
  id: number;
  name: string;
  email?: string;  // オプショナル
  readonly createdAt: Date;  // 読み取り専用
}

// Type Alias
type Point = { x: number; y: number };
type Handler = (event: Event) => void;
```

### ジェネリクス

```typescript
// 関数のジェネリクス
function first<T>(items: T[]): T | undefined {
  return items[0];
}

// 制約付きジェネリクス
function getProperty<T, K extends keyof T>(obj: T, key: K): T[K] {
  return obj[key];
}

// クラスのジェネリクス
class Container<T> {
  constructor(private value: T) {}

  getValue(): T {
    return this.value;
  }
}

// ジェネリックインターフェース
interface Repository<T> {
  findById(id: string): Promise<T | null>;
  save(entity: T): Promise<T>;
  delete(id: string): Promise<void>;
}
```

### ユーティリティ型

```typescript
interface User {
  id: number;
  name: string;
  email: string;
  age: number;
}

// Partial: すべてのプロパティをオプショナルに
type UpdateUser = Partial<User>;

// Required: すべてのプロパティを必須に
type RequiredUser = Required<User>;

// Pick: 特定のプロパティのみ抽出
type UserPreview = Pick<User, 'id' | 'name'>;

// Omit: 特定のプロパティを除外
type UserWithoutAge = Omit<User, 'age'>;

// Record: キーと値の型を指定
type UserMap = Record<string, User>;

// Readonly: すべてのプロパティを読み取り専用に
type ImmutableUser = Readonly<User>;
```

## エラーハンドリング

```typescript
// カスタムエラー
class ValidationError extends Error {
  constructor(
    message: string,
    public readonly field: string,
  ) {
    super(message);
    this.name = 'ValidationError';
  }
}

// Result型パターン
type Result<T, E = Error> =
  | { success: true; data: T }
  | { success: false; error: E };

function divide(a: number, b: number): Result<number, string> {
  if (b === 0) {
    return { success: false, error: 'Division by zero' };
  }
  return { success: true, data: a / b };
}

// try-catch
async function fetchData<T>(url: string): Promise<T> {
  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP error: ${response.status}`);
    }
    return await response.json() as T;
  } catch (error) {
    if (error instanceof Error) {
      throw new Error(`Fetch failed: ${error.message}`);
    }
    throw error;
  }
}
```

## クラス設計

```typescript
// インターフェースの実装
interface Logger {
  log(message: string): void;
  error(message: string): void;
}

class ConsoleLogger implements Logger {
  log(message: string): void {
    console.log(`[LOG] ${message}`);
  }

  error(message: string): void {
    console.error(`[ERROR] ${message}`);
  }
}

// 抽象クラス
abstract class BaseRepository<T> {
  abstract findById(id: string): Promise<T | null>;
  abstract save(entity: T): Promise<T>;

  async exists(id: string): Promise<boolean> {
    const entity = await this.findById(id);
    return entity !== null;
  }
}

// privateとreadonly
class Config {
  private static instance: Config | null = null;

  private constructor(
    public readonly apiUrl: string,
    public readonly timeout: number,
  ) {}

  static getInstance(): Config {
    if (!Config.instance) {
      Config.instance = new Config(
        process.env.API_URL ?? 'http://localhost:3000',
        Number(process.env.TIMEOUT) || 5000,
      );
    }
    return Config.instance;
  }
}
```

## テスト

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';

// 基本的なテスト
describe('Calculator', () => {
  it('should add two numbers', () => {
    expect(add(2, 3)).toBe(5);
  });

  it('should throw on division by zero', () => {
    expect(() => divide(1, 0)).toThrow('Division by zero');
  });
});

// モック
describe('UserService', () => {
  const mockRepository = {
    findById: vi.fn(),
    save: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should find user by id', async () => {
    mockRepository.findById.mockResolvedValue({ id: '1', name: 'Test' });

    const service = new UserService(mockRepository);
    const user = await service.getUser('1');

    expect(user).toEqual({ id: '1', name: 'Test' });
    expect(mockRepository.findById).toHaveBeenCalledWith('1');
  });
});

// 非同期テスト
describe('AsyncOperations', () => {
  it('should fetch data', async () => {
    const data = await fetchData('/api/users');
    expect(data).toBeDefined();
  });
});
```

## 高度なパターン

### 型ガード

```typescript
interface Dog {
  kind: 'dog';
  bark(): void;
}

interface Cat {
  kind: 'cat';
  meow(): void;
}

type Pet = Dog | Cat;

// 型述語（Type Predicate）
function isDog(pet: Pet): pet is Dog {
  return pet.kind === 'dog';
}

function handlePet(pet: Pet): void {
  if (isDog(pet)) {
    pet.bark();  // TypeScriptはpetがDogだと認識
  } else {
    pet.meow();
  }
}
```

### Zodによるバリデーション

```typescript
import { z } from 'zod';

const UserSchema = z.object({
  id: z.string().uuid(),
  name: z.string().min(1).max(100),
  email: z.string().email(),
  age: z.number().int().positive().optional(),
});

type User = z.infer<typeof UserSchema>;

function validateUser(data: unknown): User {
  return UserSchema.parse(data);
}

// Safe parse
function safeValidateUser(data: unknown): Result<User, z.ZodError> {
  const result = UserSchema.safeParse(data);
  if (result.success) {
    return { success: true, data: result.data };
  }
  return { success: false, error: result.error };
}
```

### Dependency Injection（tsyringe）

```typescript
import { container, injectable, inject } from 'tsyringe';

@injectable()
class DatabaseService {
  connect(): void {
    console.log('Connected to database');
  }
}

@injectable()
class UserService {
  constructor(
    @inject('DatabaseService') private db: DatabaseService,
  ) {}

  getUsers(): void {
    this.db.connect();
  }
}

container.register('DatabaseService', { useClass: DatabaseService });
const userService = container.resolve(UserService);
```

## コード品質チェック

実装後に確認:
- tsc --noEmit を通過するか（型チェック）
- eslint / biome check を通過するか
- prettier --check を通過するか（フォーマット）
- テストが通過するか

## リファレンス

詳細なガイドは以下を参照:

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md)
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md)
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md)
