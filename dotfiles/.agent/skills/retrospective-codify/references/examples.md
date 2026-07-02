# Retrospective Codify — 具体例と提示例

## 具体例

### 例 1: ast-grep ルール化（機械検出可能）

- 最初の試行: TypeScript で集合のサイズを `Array.from(set).length` で取得していたが、レビューで非効率と指摘された。
- 最終解: `set.size` を使う。
- 気付き: `Set` / `Map` のサイズ取得は `.size` プロパティを使う。`Array.from(...).length` は構文レベルで検出可能。

→ `rules/no-array-from-size.yml` を追加:
```yaml
id: no-array-from-size
language: TypeScript
severity: warning
rule:
  pattern: Array.from($COLL).length
message: Set/Map のサイズは .size プロパティを使う。
```

### 例 2: CLAUDE.md ルール化（短い常時ルール）

- 最初の試行: `pnpm install` を実行したら lockfile 形式の差分で CI が落ちた。
- 最終解: pnpm のバージョンを v10 系に揃えた。
- 気付き: pnpm はバージョン差で lockfile が変わる。常に v10 以上を使う。

→ `~/.claude/CLAUDE.md` の「ツール」節に追記:
```markdown
- pnpm は v10 以上を使う（理由: lockfile 形式が v9 以前と非互換で CI 差分が出る）
```

### 例 3: 新規 skill 化（手順 + 判断を伴う）

- 最初の試行: MoonBit から C ライブラリを呼ぶのに、いくつかの方法を試して FFI 宣言と stub の配置で詰まった。
- 最終解: `extern "c"` 宣言 + `moonbit.h` を使った stub + `moon.pkg.json` の `native-stub` / `link.native` 設定の組み合わせ。
- 気付き: 単一手順では収まらず、宣言・stub・ビルド設定の 3 層を一括して理解する必要がある。

→ 新規 skill `moonbit-c-binding` として手順とテンプレを切り出し（既に存在するため、本例は「重複チェックで既存への追記」を選ぶケース）。

## 提示例

### 全学びが既存カバー（重複検出のみ）

```
## Retrospective

### 学び 1: <ラベル>
- 最初の失敗: ...
- 最終解: ...
- 気付き: ...

## 提案

重複検出（提案不要）:
- 学び 1: 既存 skill `<skill 名>` の `<節名>` が完全カバー → 追加なし

採用候補なし。記録目的でレビューしてください。
```

### 部分重複（既存追記 + 重複検出）

```
## 提案

採用候補:
- [skill 追記] <既存 skill 名>: <新規部分の 1 行>（学び 1 由来, 既存節 `<節名>` への補完）

重複検出（提案不要）:
- 学び 1（version 値部分）: 既存 `~/.claude/CLAUDE.md` ツール節が既にカバー → 追記不要
```
