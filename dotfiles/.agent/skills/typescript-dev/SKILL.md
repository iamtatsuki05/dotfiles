---
name: typescript-dev
description: "Use when the user asks to implement, refactor, test, debug, or review TypeScript/TSX code, type definitions, Jest/Vitest tests, ESLint/Biome/Prettier issues, Zod validation, or TypeScript build errors."
---

# TypeScript開発スキル

TypeScriptコードの実装、テスト、デバッグ、リファクタリングを効率的に行うためのガイド。

## 実装前の必須確認

**tsconfig.json/package.jsonを必ず確認する。** プロジェクトの設定に従う。

確認項目:
- `tsconfig.json`: target, module, strict, paths, baseUrl
- `package.json`: type（"module"/"commonjs"）, scripts
- `.eslintrc`/`eslint.config.js`: ESLint設定（ESLint 9+ は flat config（`eslint.config.js`）が既定。`.eslintrc` は legacy 形式）
- `.prettierrc`: フォーマット設定
- `biome.json`: Biome使用時の設定
- 既存のテストランナー（Jest / Vitest / Playwright 等）と `package.json` scripts
- React / Node / library / CLI など実行環境
- 既存の型設計、validation、DI、エラー処理のパターン

ESLint、Biome、Prettier が併存する場合は、`package.json` scripts と既存CIで使われるものを優先する。`any` や型アサーションは既存方針に従い、必要な場合は理由を明確にする。

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

既存の型から派生型を作るときは、手書きで再定義せず組み込みユーティリティ型を使う。

- `Partial<T>` / `Required<T>`: 全プロパティをオプショナル / 必須に
- `Pick<T, K>` / `Omit<T, K>`: 特定プロパティの抽出 / 除外
- `Record<K, V>`: キーと値の型を指定したオブジェクト型
- `Readonly<T>`: 全プロパティを読み取り専用に

使用例は [references/common-patterns.md](references/common-patterns.md) を参照。

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

詳細なコード例（型ガード、Zod、tsyringe / 手動 DI、Result/Option 型、リトライ等）は [references/common-patterns.md](references/common-patterns.md) を参照。判断基準:

- **型ガード**: union 型の絞り込みには型アサーションではなく型述語（`pet is Dog`）や判別可能 union（`kind` フィールド）を使う。
- **Zod**: 外部入力（API レスポンス、環境変数、フォーム）の検証と型推論（`z.infer`）に使う。v3 と v4 で API が一部異なるため、プロジェクトの依存バージョンを確認してから書く。
- **DI**: tsyringe 等のコンテナはプロジェクトで既に採用されている場合に使い、小規模ならファクトリ関数による手動 DI で十分。

最小例（型述語）:

```typescript
type Pet = Dog | Cat; // 各型は kind: 'dog' | 'cat' で判別

function isDog(pet: Pet): pet is Dog {
  return pet.kind === 'dog';
}
```

## エンジニアリング作法（共通）

Small CL、テスト同梱、Why コメント、PR description の共通規範は `eng-practices` スキルを参照する。
TypeScript では特に、`any` や型アサーションを使う箇所に理由を残し、公開 API の型変更は PR の影響範囲に明記する。

## コード品質チェック

実装後に確認:
- tsc --noEmit を通過するか（型チェック）
- eslint / biome check を通過するか
- prettier --check を通過するか（フォーマット）
- テストが通過するか
- 変更に対応する単体テストまたはコンポーネントテストを追加・更新したか。難しい場合は理由と代替検証を報告する
- 実行不能な検証があれば、コマンド、失敗理由、未確認リスクを最終報告に含める

## リファレンス

詳細なガイドは以下を参照:

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md)
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md)
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md)
