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

既存の型から派生型を作るときは、手書きで再定義せず組み込みユーティリティ型を使う。

- `Partial<T>` / `Required<T>`: 全プロパティをオプショナル / 必須に
- `Pick<T, K>` / `Omit<T, K>`: 特定プロパティの抽出 / 除外
- `Record<K, V>`: キーと値の型を指定したオブジェクト型
- `Readonly<T>`: 全プロパティを読み取り専用に

使用例は [references/common-patterns.md](references/common-patterns.md) を参照。

## エラーハンドリング

- カスタムエラーは `Error` を継承し、`this.name` にクラス名を設定する（instanceof 判定とログの識別のため）。
- Result 型パターンを導入するかは既存プロジェクトの方針に従う。方針がなければ例外ベースを既定とする。
- `fetch` 等の外部呼び出しの失敗は握りつぶさず、URL やステータスなど失敗時の文脈を付けて再 throw する。
- 詳細例（カスタムエラー、Result/Option 型）は [references/common-patterns.md](references/common-patterns.md) を参照。

## クラス設計

アクセス修飾子、コンストラクタパラメータプロパティ、インターフェース実装・抽象クラスの例は [references/coding-standards.md](references/coding-standards.md) の「クラス設計」を参照。シングルトン等のデザインパターンは [references/common-patterns.md](references/common-patterns.md) を参照。

## テスト

- Vitest / Jest の基本、モック、スパイ、非同期テスト、パラメータ化の例は [references/testing-guide.md](references/testing-guide.md) を参照。
- テストランナーは新規導入せず、プロジェクト既存のもの（`package.json` scripts）に従う。モックは `beforeEach` でリセットし、テスト間の状態共有を避ける。

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

- **コーディング規約詳細**: [references/coding-standards.md](references/coding-standards.md) — 命名規則、interface vs type、クラス設計、ESLint/Biome ルール（no-explicit-any 等）への対応方法が必要なとき
- **テストガイド**: [references/testing-guide.md](references/testing-guide.md) — Vitest/Jest の基本、モック、非同期テスト、フィクスチャの実装例が必要なとき
- **よく使うパターン集**: [references/common-patterns.md](references/common-patterns.md) — 型ガード、Zod、DI、Result/Option 型、リトライ、デザインパターンの実装例が必要なとき
