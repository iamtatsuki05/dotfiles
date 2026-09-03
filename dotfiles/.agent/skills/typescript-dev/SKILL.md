---
name: typescript-dev
description: "Use when the user asks to implement, refactor, test, debug, or review TypeScript/TSX code, type definitions, Jest/Vitest tests, ESLint/Biome/Prettier findings, Zod validation, or tsc build errors. Read once per session. Do not use for HTML/CSS layout questions (modern-web-guidance), CI YAML, or Markdown docs."
---

# TypeScript 開発スキル

TypeScript / TSX の実装、テスト、デバッグ、リファクタリングを、対象プロジェクトの `tsconfig` とツール構成に合わせて進めるための手順と判断基準。テスト先行と互換レイヤの扱いも本文に含む。

この skill は session につき 1 回だけ読む。compaction 後は `checkpoint.md` の「適用中の skill と解決済みの実行形」を見て続け、SKILL.md を読み直さない。`$typescript-dev` で本文が注入済みの場合も再読しない。

## 着手前の確認

次の順に読み、決めた内容を `checkpoint.md` に 1〜2 行で残す。repo が無く相談だけの依頼では設定ファイルを探さず、前提にしたバージョンや設定を回答に明記する。

1. `package.json`: `type`(`module` / `commonjs`)、`scripts`(lint / test / typecheck / build の実行形)、package manager(`packageManager` フィールドか lockfile: `pnpm-lock.yaml` / `yarn.lock` / `package-lock.json` / `bun.lock`(旧 `bun.lockb`))。scripts にあるコマンドをそのまま実行形にする。
2. `tsconfig.json`: `strict`、`noUncheckedIndexedAccess`、`exactOptionalPropertyTypes`、`module` / `moduleResolution`、`paths`。`verbatimModuleSyntax` が有効なら `import type` を使う。
3. lint / format: `eslint.config.*`(ESLint 9+ の flat config)、`.eslintrc*`(legacy)、`biome.json`、`.prettierrc`。併存する場合は `scripts` と CI で実際に使われているものだけを使う。
4. テストランナー: `vitest.config.*` / `jest.config.*` / `playwright.config.*`。新規導入せず既存のものに従う。
5. 変更対象の周辺コード: 実行環境(React / Node / library / CLI)、エラー処理(例外か Result 型か)、validation(Zod のバージョン)、DI の有無、`any` と型アサーションの扱い。既存パターンはこの skill の既定より優先する。

## 進め方

1. 振る舞いの変更ごとに、失敗するテストを先に書き、`<runner> <file>`(例: `pnpm vitest run src/foo.test.ts`)で失敗を確認してから最小の実装で通し、その後に整理する。
   - 対象外: config / CI の修正、生成コード、型のみの変更で既存テストが型検査に含まれる場合。これらは既存の check を回すだけにする。
2. alias、silent fallback、default 値補完、legacy path、非等価な代替経路は、明示要件か既存契約がなければ追加せず、明確なエラーを投げる。実際に書きそうになった時点でだけ `compatibility-safety` を読む。
3. lint・型エラーはコード側を直す。`any`、`as`、`!`、`eslint-disable`、`@ts-expect-error` は理由をコメントに残せる場合だけ使い、設定の緩和で通さない。
4. 公開 API、Props、export した型を変えたら、呼び出し元、stories、unit / e2e テストを同じ変更で同期する。同期漏れは `tsc --noEmit` の全体実行で連鎖エラーとして出る。

## 規約と判断基準

プロジェクトに規約や既存パターンがあればそれに従う。無い場合の既定:

- 既存の型から派生型を作るときは手書きで再定義せず、`Partial` / `Required` / `Pick` / `Omit` / `Record` / `Readonly` を使う。
- union の絞り込みは型アサーションでなく、型述語(`pet is Dog`)か判別可能 union(`kind` フィールド)で行う。
- カスタムエラーは `Error` を継承し `this.name` にクラス名を設定する。`fetch` などの外部呼び出しの失敗は握りつぶさず、URL やステータスを付けて再 throw する。Result / Option 型は既存方針がある場合だけ導入し、無ければ例外ベース。実装例は [references/common-patterns.md](references/common-patterns.md) の「エラーハンドリングパターン」と「関数型パターン」。
- 外部入力(API レスポンス、環境変数、フォーム)の検証は Zod を使い、型は `z.infer` から導く。v3 と v4 で API が一部異なるので、依存バージョンを確認してから書く。実装例は同ファイルの「バリデーション」。
- DI コンテナ(tsyringe など)はプロジェクトが採用済みの場合だけ使い、小規模ならファクトリ関数による手動 DI で足りる。
- `interface` と `type` の使い分け、アクセス修飾子、抽象クラス、ESLint / Biome の個別ルール(`no-explicit-any`、`no-floating-promises`、`prefer-nullish-coalescing` など)の直し方は [references/coding-standards.md](references/coding-standards.md) を見る。

テストの既定:

- モックは `beforeEach` で `vi.resetAllMocks()` / `jest.resetAllMocks()`(または config の `mockReset: true`)によりリセットし、テスト間の状態共有を避ける。`restoreAllMocks` は `spyOn` の復元だけで `vi.fn` の状態は戻らない。
- 類似ケースは `test.each` / `it.each` にまとめる。
- 非同期は `await expect(promise).rejects.toThrow(...)` の形で検証し、未 await の Promise を残さない。
- モジュールモック、スパイ、カスタムマッチャー、フィクスチャの書き方は [references/testing-guide.md](references/testing-guide.md) を見る。

## 完了前の確認

`package.json` の scripts を優先し、無ければ次を実行する(`pnpm` の例。package manager は lockfile に合わせる)。

```bash
pnpm tsc --noEmit
pnpm eslint .            # Biome なら pnpm biome check .
pnpm prettier --check .  # Prettier を使う場合
pnpm vitest run <変更に近い test>   # 通ったら全体。Jest なら pnpm jest
```

- 変更した振る舞いに対応する unit / component テストを追加・更新したか。難しい場合は理由と代替の検証を報告に書く。
- 実行できない検証は、コマンド、失敗理由、残るリスクを報告に回す。

## 報告に含めること

- 変更ファイルと目的。規約の根拠(`tsconfig` / lint 設定か、この skill の既定か)。
- 実行した検証コマンドと結果(失敗 0 件、テスト件数など)。未実行の検証とその理由。
- 追加・更新したテスト。公開 API や型を変えた場合は影響範囲。
- `any`、型アサーション、互換レイヤを入れた場合は、その根拠と削除条件。
- PR description を書く段階になったら `eng-practices` を読む。それ以外の場面では読まない。
