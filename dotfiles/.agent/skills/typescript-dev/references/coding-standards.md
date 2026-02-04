# コーディング規約詳細

## 目次

1. [tsconfig.json参照](#tsconfigjson参照)
2. [命名規則](#命名規則)
3. [型定義](#型定義)
4. [インポート](#インポート)
5. [クラス設計](#クラス設計)
6. [ESLint/Biomeルール対応](#eslintbiomeルール対応)

## tsconfig.json参照

実装前に必ずプロジェクトのtsconfig.jsonを確認する。主要な設定項目:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["src/*"]
    }
  }
}
```

## 命名規則

```typescript
// ファイル名: kebab-case または camelCase
// user-service.ts / userService.ts

// クラス: PascalCase
class UserAuthentication {}

// インターフェース: PascalCase（I接頭辞は不要）
interface User {}
interface UserRepository {}

// 型エイリアス: PascalCase
type UserId = string;
type UserStatus = 'active' | 'inactive';

// 関数・メソッド: camelCase
function validateUserInput(data: unknown): boolean {
  return true;
}

// 変数: camelCase
const userCount = 0;
const isValid = true;

// 定数: UPPER_SNAKE_CASE または camelCase
const MAX_RETRY_COUNT = 3;
const DEFAULT_TIMEOUT = 30;

// private: # または _ 接頭辞
class Service {
  #internalCache: Map<string, unknown> = new Map();
  private _legacyField = '';
}

// Enum: PascalCase（キーもPascalCase）
enum UserRole {
  Admin = 'admin',
  User = 'user',
  Guest = 'guest',
}

// const enumは使用禁止（バンドル問題があるため）
```

## 型定義

### interface vs type

```typescript
// オブジェクトの形状にはinterfaceを使用
interface User {
  id: string;
  name: string;
  email: string;
}

// 拡張が必要な場合
interface AdminUser extends User {
  permissions: string[];
}

// Union型、関数型、プリミティブのエイリアスにはtypeを使用
type UserId = string;
type Status = 'pending' | 'success' | 'error';
type Handler = (event: Event) => void;
type Result<T> = { success: true; data: T } | { success: false; error: Error };
```

### 厳密な型定義

```typescript
// anyの使用を避ける
// NG
function process(data: any): any {
  return data;
}

// OK: unknownを使用して型を絞り込む
function process(data: unknown): string {
  if (typeof data === 'string') {
    return data;
  }
  throw new Error('Invalid data type');
}

// OK: ジェネリクスを使用
function process<T>(data: T): T {
  return data;
}

// 型アサーションよりも型ガードを優先
// NG
const user = data as User;

// OK
function isUser(data: unknown): data is User {
  return (
    typeof data === 'object' &&
    data !== null &&
    'id' in data &&
    'name' in data
  );
}

if (isUser(data)) {
  console.log(data.name);
}
```

### Nullチェック

```typescript
// Optionalプロパティ
interface Config {
  timeout?: number;  // number | undefined
}

// Nullable
interface Response {
  data: User | null;  // 明示的にnullを許容
}

// Non-null assertion（!）は避ける
// NG
const name = user!.name;

// OK: 適切なチェックを行う
if (user) {
  const name = user.name;
}

// Nullish coalescing
const timeout = config.timeout ?? 5000;

// Optional chaining
const email = user?.profile?.email;
```

## インポート

```typescript
// Node.js内蔵モジュール
import { readFile } from 'node:fs/promises';
import path from 'node:path';

// サードパーティ
import { z } from 'zod';
import express from 'express';

// エイリアスパス（tsconfig.jsonのpaths設定に基づく）
import { UserService } from '@/services/user-service';
import { User } from '@/types/user';

// 相対パス（同一ディレクトリ内のみ）
import { helper } from './helper';
```

順序:
1. Node.js内蔵モジュール（node: プレフィックス付き）
2. サードパーティモジュール
3. エイリアスパス（@/）
4. 相対パス（./）

### 型のインポート

```typescript
// 型のみのインポートはimport typeを使用
import type { User, UserRole } from '@/types/user';

// 値と型を混在させる場合
import { UserService, type UserServiceOptions } from '@/services/user-service';
```

## クラス設計

### アクセス修飾子

```typescript
class UserService {
  // publicは省略可能だが明示することも可
  public readonly name: string;

  // privateフィールド（#を推奨）
  #repository: UserRepository;

  // protectedは継承時のみ
  protected logger: Logger;

  constructor(
    repository: UserRepository,
    logger: Logger,
  ) {
    this.name = 'UserService';
    this.#repository = repository;
    this.logger = logger;
  }

  // publicメソッド
  async getUser(id: string): Promise<User | null> {
    this.logger.info(`Getting user: ${id}`);
    return this.#repository.findById(id);
  }

  // privateメソッド
  #validateId(id: string): boolean {
    return id.length > 0;
  }
}
```

### コンストラクタパラメータプロパティ

```typescript
// コンパクトな書き方
class Service {
  constructor(
    private readonly repository: Repository,
    private readonly logger: Logger,
  ) {}
}

// 上記は以下と同等
class Service {
  private readonly repository: Repository;
  private readonly logger: Logger;

  constructor(repository: Repository, logger: Logger) {
    this.repository = repository;
    this.logger = logger;
  }
}
```

## ESLint/Biomeルール対応

### @typescript-eslint/no-explicit-any

```typescript
// NG
function parse(data: any): any {
  return JSON.parse(data);
}

// OK
function parse<T>(data: string): T {
  return JSON.parse(data) as T;
}

// どうしても必要な場合はコメントで無効化
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function legacyHandler(data: any): void {
  // ...
}
```

### @typescript-eslint/no-unused-vars

```typescript
// NG: 未使用の変数
const unused = 'value';

// OK: 使用する場合
const used = 'value';
console.log(used);

// OK: アンダースコア接頭辞で意図的な未使用を示す
function handler(_event: Event, data: string): void {
  console.log(data);
}
```

### @typescript-eslint/explicit-function-return-type

```typescript
// NG: 戻り値型なし
function add(a: number, b: number) {
  return a + b;
}

// OK: 戻り値型あり
function add(a: number, b: number): number {
  return a + b;
}

// OK: アロー関数の場合も同様
const multiply = (a: number, b: number): number => a * b;
```

### no-floating-promises

```typescript
// NG: Promiseを無視
async function fetchData(): Promise<void> {
  fetch('/api/data');  // 戻り値を無視
}

// OK: awaitする
async function fetchData(): Promise<void> {
  await fetch('/api/data');
}

// OK: void演算子で意図的に無視を明示
function triggerFetch(): void {
  void fetch('/api/data');
}
```

### prefer-nullish-coalescing

```typescript
// NG: ||演算子（falsy値の問題）
const value = input || 'default';  // inputが0や''でもdefaultになる

// OK: ??演算子（null/undefinedのみ）
const value = input ?? 'default';
```
