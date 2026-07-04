---
name: database-dev
description: "Use when the user asks to design or modify database schemas, queries, indexes, migrations, transactions, EXPLAIN plans, N+1 issues, SQL/NoSQL modeling, or database performance behavior."
---

# データベース開発スキル

データベース設計、クエリ最適化、migration、パフォーマンス改善を効率的に行うためのガイド。

## 実装前の必須確認

**既存のスキーマと設定を確認する。** データベースの種類、バージョン、既存のテーブル構造を把握する。

確認項目:
- DBエンジン: PostgreSQL, MySQL, SQLite, MongoDB等
- ORMの有無: SQLAlchemy, Prisma, TypeORM, ActiveRecord等
- migration ツール: Alembic, Flyway, Knex, Prisma Migrate等
- 既存のスキーマ定義ファイル
- 対象環境: local / dev / staging / production
- データ量、許容ロック時間、バックアップ/rollback 方針

本番または共有環境に影響する migration、不可逆変更、長時間 lock、データ削除・大量更新は、実行前に影響と戻し方を示してユーザー承認を取る。

## スキーマ設計

### 正規化レベル

実務では3NFまでを目標とし、パフォーマンス要件に応じた意図的な非正規化は理由を記録する。各正規形の定義・変換例・非正規化パターンは [references/normalization.md](references/normalization.md) を参照。

### テーブル設計とリレーション

- 制約（NOT NULL, UNIQUE, FK, CHECK）はアプリ層任せにせず DB 層で守る。
- `updated_at` の自動更新は ORM/アプリ層かトリガーで行う。PostgreSQL のトリガー実装例は [references/engine-specific.md](references/engine-specific.md) を参照。
- 多対多は中間テーブル + 複合主キー。FK の `ON DELETE`（CASCADE / SET NULL / RESTRICT）は業務上の親子関係で選び、無条件に CASCADE にしない。

### 型の選択指針

| 用途 | PostgreSQL | MySQL | 注意点 |
|------|------------|-------|--------|
| 主キー | UUID / BIGSERIAL | BINARY(16) / BIGINT AUTO_INCREMENT | UUIDは分散環境向き |
| 日時 | TIMESTAMPTZ | DATETIME(6) | タイムゾーン考慮 |
| 金額 | DECIMAL(p,s) | DECIMAL(p,s) | 浮動小数点は避ける |
| JSON | JSONB | JSON | PostgreSQLはJSONB推奨 |
| 列挙 | VARCHAR + CHECK | ENUM | ENUMは変更が困難 |

## インデックス設計

### インデックス選択の判断基準

- WHERE句で頻繁に使用するカラム、JOIN条件、ORDER BY / GROUP BYのカラム
- 複合インデックスは左端カラムからしか使われない。等価条件を左、範囲条件を右に置く
- カーディナリティが高いカラム優先。書き込み頻度とのトレードオフを考慮
- 特定条件の行だけ検索するなら部分インデックス、SELECT 列まで含めるならカバリング（INCLUDE）を検討

インデックスタイプ別（B-tree / GIN / GiST / BRIN）の使い分けと実例は [references/query-optimization.md](references/query-optimization.md) を参照。

## クエリ最適化

### EXPLAIN のチェックポイント

`EXPLAIN (ANALYZE, BUFFERS)` で以下を確認する:

- **Seq Scan**: 大きなテーブルでは要注意
- **Nested Loop**: 内側のテーブルが大きい場合は非効率
- **Sort**: メモリ不足でディスクソートになっていないか
- **Rows**: 推定値と実際値の乖離（乖離が大きければ `ANALYZE` で統計情報を更新）

ノードタイプ別の読み方、ジョイン・サブクエリ・ページネーションの最適化は [references/query-optimization.md](references/query-optimization.md) を参照。

### インデックスが効かない・非自明なパターン

- カラムに関数を適用した条件（`LOWER(email) = ...` 等）にはインデックスが効かない。関数インデックスを作るか、アプリ側で正規化して保存する。
- `LIKE '%キーワード%'` の中間一致は B-tree では効かない。PostgreSQL ではあいまい検索（trigram）を使う: `CREATE EXTENSION pg_trgm` が前提で、`USING gin(col gin_trgm_ops)` のインデックスを作成する。
- N+1 はループ内クエリの発火が典型。JOIN か集計サブクエリで 1 クエリにまとめ、ORM 使用時は Eager Loading 設定を確認する。

## トランザクション設計

- デフォルト分離レベルは PostgreSQL が READ COMMITTED、MySQL (InnoDB) が REPEATABLE READ で異なる。
- REPEATABLE READ での Phantom Read は MySQL では起こり得るが、PostgreSQL では snapshot isolation により発生しない。エンジンをまたぐ移植時に前提を確認する。
- デッドロック回避: 複数行・複数テーブルをロックする処理は常に同じ順序でロックを取得し、`lock_timeout` を設定する。
- トランザクションは短く保ち、外部 API 呼び出し等を中に含めない。

ロックの種類、`SKIP LOCKED` によるキュー処理などは [references/query-optimization.md](references/query-optimization.md) の「ロック最適化」を参照。

## マイグレーション

安全に進めるための判断基準:

- **カラム追加（デフォルト値なし）**: 即座に完了する安全な操作。
- **カラム追加（デフォルト値あり）**: PostgreSQL 11+ は非 volatile なデフォルト（定数など）に限り即座に完了する。volatile なデフォルト（`gen_random_uuid()` 等）や古いバージョンでは全行書き換えが発生する。MySQL はバージョンと `ALGORITHM` 指定で挙動が異なるため、対象バージョンのドキュメントを確認する。
- **大きなテーブルへのインデックス追加**: PostgreSQL では `CREATE INDEX CONCURRENTLY` を使い、書き込みロックを避ける。
- **カラム削除**: アプリからの参照削除 → NOT NULL 制約解除 → 期間を置いてカラム削除、と段階的に行う。
- **カラム名変更・型変更**: 直接の `RENAME` / `ALTER TYPE` はアプリと非互換になるため、expand-contract（新カラム追加 → データコピーと二重書き込み → 参照切替 → 旧カラム削除）で段階的に行う。

各操作の SQL 実例は [references/migrations.md](references/migrations.md) を参照。

## NoSQL (MongoDB) パターン

ドキュメント設計の判断基準:

- **埋め込み**: 1対少で、親と常に一緒に読み書きするデータ（例: ユーザーの住所一覧）。ドキュメントサイズ上限に注意。
- **参照**: 1対多・多対多で、独立してアクセス・更新するデータ（例: ユーザーと注文）。
- インデックスは RDB と同様に、複合（左端から使用）、部分、ユニーク、TTL、テキストを使い分ける。

ドキュメント設計・インデックス・アグリゲーション・トランザクションの実例は [references/engine-specific.md](references/engine-specific.md) の MongoDB 節を参照。

## パフォーマンス監視

- **PostgreSQL**: `pg_stat_statements` でスロークエリを特定する（PostgreSQL 13+ ではカラム名が `mean_exec_time` / `total_exec_time`。12 以前は `mean_time` / `total_time`）。`pg_stat_user_tables` で Seq Scan の多いテーブル、`pg_stat_user_indexes` で未使用インデックスを確認する。
- **MySQL**: スロークエリログと `performance_schema` で実行統計を確認する。

具体的な監視 SQL は [references/engine-specific.md](references/engine-specific.md) の各エンジン節を参照。

## マイグレーション運用（eng-practices）

スキーマ変更／migration に固有の運用:

- **1 migration は 1 目的に絞る**: 複数テーブル横断の変更は段階分割を検討する。
- **影響範囲と戻し方を明示**: PR 本文に対象テーブル、想定 lock 時間、データ量、ロールバック手順、必要なら段階的リリース計画を書く。

Small CL、Why の残し方、テスト同梱などの共通原則は `eng-practices` スキルを参照。

## コード品質チェック

実装後に確認:
- スキーマに適切な制約（NOT NULL, UNIQUE, FK, CHECK）があるか
- インデックスが適切に設定されているか
- N+1クエリが発生していないか
- migration がロールバック可能か
- 本番データ量でのパフォーマンステスト
- migration dry-run、テストDBへの適用、`EXPLAIN` / `EXPLAIN ANALYZE` の確認ができたか
- 最終報告に変更内容、互換性影響、lock/rollback の見通し、実行した検証、未検証リスクを含める

## リファレンス

詳細なガイドは以下を参照:

- **正規化とモデリング**: [references/normalization.md](references/normalization.md)
- **クエリ最適化詳細**: [references/query-optimization.md](references/query-optimization.md)
- **安全な migration 実例**: [references/migrations.md](references/migrations.md)
- **各DBエンジン固有のTips（監視SQL、MongoDB実例を含む）**: [references/engine-specific.md](references/engine-specific.md)
