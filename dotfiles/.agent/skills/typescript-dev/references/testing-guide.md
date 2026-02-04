# テストガイド

## 目次

1. [テストフレームワーク](#テストフレームワーク)
2. [テストファイル構成](#テストファイル構成)
3. [基本的なテスト](#基本的なテスト)
4. [モック](#モック)
5. [非同期テスト](#非同期テスト)
6. [テストユーティリティ](#テストユーティリティ)

## テストフレームワーク

### Vitest（推奨）

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.spec.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
    },
  },
});
```

### Jest

```typescript
// jest.config.ts
import type { Config } from 'jest';

const config: Config = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/*.test.ts', '**/*.spec.ts'],
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
  },
};

export default config;
```

## テストファイル構成

```
src/
├── services/
│   ├── user-service.ts
│   └── user-service.test.ts    # 同一ディレクトリに配置
├── utils/
│   ├── validator.ts
│   └── validator.test.ts
└── __tests__/                   # 統合テスト
    └── integration/
        └── api.test.ts
```

## 基本的なテスト

### Vitest

```typescript
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Calculator } from './calculator';

describe('Calculator', () => {
  let calculator: Calculator;

  beforeEach(() => {
    calculator = new Calculator();
  });

  afterEach(() => {
    // クリーンアップ
  });

  describe('add', () => {
    it('should add two positive numbers', () => {
      expect(calculator.add(2, 3)).toBe(5);
    });

    it('should handle negative numbers', () => {
      expect(calculator.add(-1, 1)).toBe(0);
    });
  });

  describe('divide', () => {
    it('should divide two numbers', () => {
      expect(calculator.divide(10, 2)).toBe(5);
    });

    it('should throw on division by zero', () => {
      expect(() => calculator.divide(1, 0)).toThrow('Division by zero');
    });
  });
});
```

### マッチャー

```typescript
// 等価性
expect(value).toBe(5);            // 厳密等価（===）
expect(obj).toEqual({ a: 1 });    // 深い等価性
expect(obj).toStrictEqual({ a: 1 }); // より厳密な等価性

// 真偽値
expect(value).toBeTruthy();
expect(value).toBeFalsy();
expect(value).toBeNull();
expect(value).toBeUndefined();
expect(value).toBeDefined();

// 数値
expect(value).toBeGreaterThan(3);
expect(value).toBeGreaterThanOrEqual(3);
expect(value).toBeLessThan(5);
expect(value).toBeCloseTo(0.3, 5);  // 浮動小数点

// 文字列
expect(str).toMatch(/pattern/);
expect(str).toContain('substring');

// 配列
expect(arr).toContain(item);
expect(arr).toHaveLength(3);

// オブジェクト
expect(obj).toHaveProperty('key');
expect(obj).toHaveProperty('key', 'value');

// 例外
expect(() => fn()).toThrow();
expect(() => fn()).toThrow('error message');
expect(() => fn()).toThrow(CustomError);
```

## モック

### 関数モック

```typescript
import { describe, it, expect, vi } from 'vitest';

describe('UserService', () => {
  it('should call repository', async () => {
    // モック関数の作成
    const mockFn = vi.fn();
    mockFn.mockReturnValue('result');

    // 非同期モック
    const asyncMock = vi.fn();
    asyncMock.mockResolvedValue({ id: '1', name: 'Test' });

    // 呼び出し確認
    await asyncMock('arg1');
    expect(asyncMock).toHaveBeenCalled();
    expect(asyncMock).toHaveBeenCalledWith('arg1');
    expect(asyncMock).toHaveBeenCalledTimes(1);
  });

  it('should mock implementation', () => {
    const mockFn = vi.fn().mockImplementation((x: number) => x * 2);
    expect(mockFn(5)).toBe(10);
  });
});
```

### モジュールモック

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';

// モジュール全体をモック
vi.mock('./repository', () => ({
  UserRepository: vi.fn().mockImplementation(() => ({
    findById: vi.fn().mockResolvedValue({ id: '1', name: 'Test' }),
    save: vi.fn().mockResolvedValue({ id: '1', name: 'Test' }),
  })),
}));

// 部分モック
vi.mock('./utils', async () => {
  const actual = await vi.importActual('./utils');
  return {
    ...actual,
    specificFunction: vi.fn().mockReturnValue('mocked'),
  };
});

describe('with mocked module', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should use mocked repository', async () => {
    const { UserRepository } = await import('./repository');
    const repo = new UserRepository();
    const user = await repo.findById('1');
    expect(user).toEqual({ id: '1', name: 'Test' });
  });
});
```

### スパイ

```typescript
import { describe, it, expect, vi } from 'vitest';

describe('spying', () => {
  it('should spy on object method', () => {
    const obj = {
      method: (x: number) => x * 2,
    };

    const spy = vi.spyOn(obj, 'method');
    obj.method(5);

    expect(spy).toHaveBeenCalledWith(5);
    spy.mockRestore();
  });

  it('should spy on console', () => {
    const consoleSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    console.log('test');
    expect(consoleSpy).toHaveBeenCalledWith('test');
    consoleSpy.mockRestore();
  });
});
```

## 非同期テスト

```typescript
import { describe, it, expect, vi } from 'vitest';

describe('async operations', () => {
  // async/await
  it('should fetch data', async () => {
    const data = await fetchData();
    expect(data).toBeDefined();
  });

  // Promiseの解決を待つ
  it('should resolve promise', async () => {
    await expect(fetchData()).resolves.toEqual({ id: 1 });
  });

  // Promiseの拒否を待つ
  it('should reject promise', async () => {
    await expect(failingOperation()).rejects.toThrow('error');
  });

  // タイマーのモック
  it('should handle timers', async () => {
    vi.useFakeTimers();

    const callback = vi.fn();
    setTimeout(callback, 1000);

    vi.advanceTimersByTime(1000);
    expect(callback).toHaveBeenCalled();

    vi.useRealTimers();
  });

  // fetch のモック
  it('should mock fetch', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ data: 'test' }),
    });

    const response = await fetch('/api/data');
    const data = await response.json();
    expect(data).toEqual({ data: 'test' });
  });
});
```

## テストユーティリティ

### カスタムマッチャー

```typescript
// vitest.setup.ts
import { expect } from 'vitest';

expect.extend({
  toBeValidEmail(received: string) {
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    const pass = emailRegex.test(received);
    return {
      pass,
      message: () =>
        pass
          ? `expected ${received} not to be a valid email`
          : `expected ${received} to be a valid email`,
    };
  },
});

// 使用
it('should be valid email', () => {
  expect('test@example.com').toBeValidEmail();
});
```

### テストファクトリ

```typescript
// test/factories/user.ts
interface User {
  id: string;
  name: string;
  email: string;
  role: 'admin' | 'user';
}

export function createUser(overrides: Partial<User> = {}): User {
  return {
    id: 'user-1',
    name: 'Test User',
    email: 'test@example.com',
    role: 'user',
    ...overrides,
  };
}

// 使用
it('should handle admin user', () => {
  const admin = createUser({ role: 'admin' });
  expect(admin.role).toBe('admin');
});
```

### テストフィクスチャ

```typescript
import { beforeEach, afterEach } from 'vitest';

interface TestContext {
  db: Database;
  server: Server;
}

// フィクスチャの設定
beforeEach<TestContext>(async (context) => {
  context.db = await createTestDatabase();
  context.server = await createTestServer(context.db);
});

afterEach<TestContext>(async (context) => {
  await context.server.close();
  await context.db.cleanup();
});

it<TestContext>('should query database', async ({ db }) => {
  const result = await db.query('SELECT * FROM users');
  expect(result).toBeDefined();
});
```

### パラメータ化テスト

```typescript
import { describe, it, expect } from 'vitest';

describe('parameterized tests', () => {
  it.each([
    { input: 1, expected: 2 },
    { input: 2, expected: 4 },
    { input: 3, expected: 6 },
  ])('should double $input to $expected', ({ input, expected }) => {
    expect(double(input)).toBe(expected);
  });

  // テーブル形式
  it.each`
    a    | b    | expected
    ${1} | ${2} | ${3}
    ${2} | ${3} | ${5}
    ${3} | ${4} | ${7}
  `('should add $a + $b = $expected', ({ a, b, expected }) => {
    expect(add(a, b)).toBe(expected);
  });
});
```
