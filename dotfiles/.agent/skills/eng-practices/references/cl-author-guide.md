# CL/PR Author 側ガイド

Google eng-practices の CL Author Guide の 3 章（Small CLs / Writing Good CL Descriptions / Handling Reviewer Comments）を、Claude Code エージェントが PR を作る・更新する際に直接使う形に整理する。

## 目次

- [Small CLs](#small-cls) — サイズ目安、分割の方針、分割できないとき
- [Writing Good CL Descriptions](#writing-good-cl-descriptions) — 構造テンプレート、タイトル、悪い例と直し方、Why を残す例
- [Handling Reviewer Comments](#handling-reviewer-comments) — 受け取り方、反映の進め方、「後で直す」の扱い
- [関連](#関連)

## Small CLs

### サイズ目安

| サイズ | 行数（追加+削除、自動生成除く） | 扱い |
|---|---|---|
| 最適 | ≤ 100 行 | レビュー速度・バグ低減・ロールバック容易 |
| 許容 | 100〜400 行 | 単一目的なら可、テスト同梱なら自然 |
| 大きい | 400〜1000 行 | 再分割を検討。説明文で分割不可な理由を書く |
| 過大 | > 1000 行 | 原則再分割。読みやすく批評可能な単位へ |

ただし「ファイル全体を移動するリネーム」「自動生成コードの更新」「import 整理」など、行数の割にレビュー負荷が低い変更は除外して考える。

### 分割の方針

- **1 CL = 1 目的**。バグ修正と機能追加を同居させない。
- **リファクタは別 CL**。「先に refactor を入れて読みやすくする → 機能を足す」の 2 段階に割る。
- **テストは同梱**。機能変更とテストを別 CL に分けない。テストだけを先に入れる「failing test CL」は例外的に許容。
- **層ごとの分割**: DB migration / モデル / API / UI / 配信フラグ を別 CL にすると、各 CL が小さく独立検証できる。
- **詰まったら問い直す**: 「先に refactor CL を入れたら、機能 CL は小さくなるか？」をまず検討する。

### 分割できないとき

- 中間状態がコンパイル/テストを通らない場合は同一 CL に閉じてよい。代わりに CL 説明で「なぜ分割不可か」を明示する。
- 1000 行超でも「全行が機械的・低リスク」なら許容するが、レビュアー負荷を下げる工夫（自動生成のラベル付け、レビュー観点の事前共有）を CL 説明に書く。

## Writing Good CL Descriptions

### 構造テンプレート

```
<命令形のタイトル：何を変えたか、具体的に>

<本文>
- 背景・動機（Why）
- 変更内容の要約（What）
- 影響範囲（公開 API、データ、運用）
- 採用しなかった代替案と理由
- 残課題・未検証項目（あれば issue/TODO リンク）
- 関連 issue / 設計ドキュメント / 過去 CL / 再現手順
```

### タイトルの書き方

- **命令形**: `Add ...` / `Fix ...` / `Remove ...` / `Refactor ...`。
- **短く、具体的に**: 50〜70 文字目安。
- **テーマと範囲がわかる**: `Fix race in user-token cache by guarding refresh with mutex` は良い。`Fix bug` は悪い。
- 接頭辞は repo の規約に従う（`fix:`, `feat:` 等）。`pr-code-review` で見たコミットメッセージ規約と整合させる。

### 悪い例と直し方

| 悪い例 | 問題 | 良い例 |
|---|---|---|
| `Bug fix` | 何の bug か不明 | `Fix null deref in OrderService.cancel when order is already shipped` |
| `Update deps` | 何を、なぜ更新したか不明 | `Bump axios to 1.7.4 to pick up SSRF fix (CVE-2024-XXXX)` |
| `Refactor` | 範囲・目的不明 | `Extract pricing calculation from OrderService into PriceEngine` |
| `WIP` | 状態しか伝えない | `Draft: switch payments to Stripe (UI only, server PR comes next)` |

### Why を残す例

```
Switch session store from Redis to Postgres

Why:
- Redis を別チームに依存していて、deploy 順序が複雑（incident #4521）。
- セッションは秒間 10 req と低トラフィックなので Postgres で十分。

What:
- SessionStore interface に PostgresSessionStore を追加。
- 既存 RedisSessionStore は feature flag `session_store` で切替可能なまま残す。
- 移行は次の CL で flag を flip。

代替案:
- DynamoDB: 別 region に持つにはコスト・運用負荷が高い。次善案。
- KeyDB: oss 移行コストが今は割に合わない。

残課題:
- TTL の自動 sweep ジョブは次の CL（#1234）。
```

## Handling Reviewer Comments

### 受け取り方

1. **個人化しない**。指摘はコードに対するもの。
2. **まずコードを改善できないか考える**: 「説明コメントを足す」より「関数名を変える」「分割する」を先に検討する。レビュアーが詰まる箇所は将来の読者も詰まる。
3. **合意形成は事実ベース**: スタイルガイド、過去 incident、測定値で議論する。
4. **反論するときは礼儀と論点**: 「なぜそうしたか」を 1〜2 行で明示し、感情を入れない。
5. **採用しないなら理由を残す**: `Optional` 系を見送るなら「今回スコープ外、issue #5678 で別途対応」と書く。空 reply で無視しない。

### 反映の進め方

- 修正は小さく commit を積む（force push しない設定なら）。force push 必須の repo では、レビュアーが「反映が見えにくい」と感じるので、変更点を PR コメントで要約する。
- 1 つのコメントスレッドに 1 つの修正を対応させる。「Done」だけでなく「変更箇所のファイル名・行」を添えるとレビュアーの再確認が早い。
- 反映後は **再レビュー依頼を明示**: 「全コメント対応しました、再レビューお願いします」とコメント。

### 「後で直す」は約束しない

- 「次の CL で直す」を口頭約束にしない。新 issue/TODO を切り、リンクを CL 説明か該当コードコメントに残す。
- 大量の Nit を「次の CL で」とまとめて流すと、いずれの CL も来ない。

### 攻撃的・建設的でないコメントへの対処

- 公開で反論しない。私的に対話し、改善がなければチーム/マネージャーへエスカレーションする。

## 関連

- レビュアー側の 6 軸: [reviewer-guide.md](reviewer-guide.md)
- コメント作法と pushback: [comment-writing.md](comment-writing.md)
- 役割別チェックリスト: [checklists.md](checklists.md)
