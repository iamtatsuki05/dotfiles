---
name: database-dev
description: "Use when the user asks to design or modify database schemas, queries, indexes, migrations, transactions, EXPLAIN plans, N+1 issues, SQL/NoSQL modeling, or database performance behavior."
---

# データベース開発スキル

データベース設計、クエリ最適化、マイグレーション、パフォーマンス改善を効率的に行うためのガイド。

## 実装前の必須確認

**既存のスキーマと設定を確認する。** データベースの種類、バージョン、既存のテーブル構造を把握する。

確認項目:
- DBエンジン: PostgreSQL, MySQL, SQLite, MongoDB等
- ORMの有無: SQLAlchemy, Prisma, TypeORM, ActiveRecord等
- マイグレーションツール: Alembic, Flyway, Knex, Prisma Migrate等
- 既存のスキーマ定義ファイル
- 対象環境: local / dev / staging / production
- データ量、許容ロック時間、バックアップ/rollback 方針

本番または共有環境に影響する migration、不可逆変更、長時間 lock、データ削除・大量更新は、実行前に影響と戻し方を示してユーザー承認を取る。

## スキーマ設計

### 正規化レベル

```
第1正規形 (1NF): 繰り返しグループを排除、原子値のみ
第2正規形 (2NF): 1NF + 部分関数従属を排除
第3正規形 (3NF): 2NF + 推移的関数従属を排除
BCNF: すべての決定項が候補キー
```

実務では3NFまでを目標とし、パフォーマンス要件に応じて意図的に非正規化する。

### テーブル設計の基本

```sql
-- PostgreSQL例
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'inactive', 'suspended')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`updated_at` の自動更新は ORM/アプリ層かトリガーで行う。PostgreSQL のトリガー実装例は [references/engine-specific.md](references/engine-specific.md) を参照。

### リレーション設計

```sql
-- 1対多
CREATE TABLE orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    total_amount DECIMAL(10, 2) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 多対多（中間テーブル）
CREATE TABLE product_categories (
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, category_id)
);

-- 自己参照（階層構造）
CREATE TABLE categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    parent_id UUID REFERENCES categories(id) ON DELETE SET NULL
);
```

### 型の選択指針

| 用途 | PostgreSQL | MySQL | 注意点 |
|------|------------|-------|--------|
| 主キー | UUID / BIGSERIAL | BINARY(16) / BIGINT AUTO_INCREMENT | UUIDは分散環境向き |
| 日時 | TIMESTAMPTZ | DATETIME(6) | タイムゾーン考慮 |
| 金額 | DECIMAL(p,s) | DECIMAL(p,s) | 浮動小数点は避ける |
| JSON | JSONB | JSON | PostgreSQLはJSONB推奨 |
| 列挙 | VARCHAR + CHECK | ENUM | ENUMは変更が困難 |

## インデックス設計

### 基本原則

```sql
-- 単一カラムインデックス
CREATE INDEX idx_users_email ON users(email);

-- 複合インデックス（左端から使われる）
CREATE INDEX idx_orders_user_created ON orders(user_id, created_at DESC);

-- 部分インデックス（条件付き）
CREATE INDEX idx_orders_pending ON orders(created_at)
    WHERE status = 'pending';

-- カバリングインデックス（INCLUDE）
CREATE INDEX idx_orders_user_covering ON orders(user_id)
    INCLUDE (total_amount, status);
```

### インデックス選択の判断基準

- WHERE句で頻繁に使用するカラム
- JOIN条件のカラム
- ORDER BY / GROUP BYのカラム
- カーディナリティが高いカラム優先
- 更新頻度とのトレードオフを考慮

## クエリ最適化

### EXPLAIN分析

```sql
-- PostgreSQL
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT u.name, COUNT(o.id) as order_count
FROM users u
LEFT JOIN orders o ON u.id = o.user_id
WHERE u.status = 'active'
GROUP BY u.id, u.name
ORDER BY order_count DESC
LIMIT 10;
```

チェックポイント:
- **Seq Scan**: 大きなテーブルでは要注意
- **Nested Loop**: 内側のテーブルが大きい場合は非効率
- **Sort**: メモリ不足でディスクソートになっていないか
- **Rows**: 推定値と実際値の乖離

### N+1問題の検出と解決

```sql
-- NG: N+1クエリ
-- 1. SELECT * FROM users;
-- 2. SELECT * FROM orders WHERE user_id = ?; (N回)

-- OK: JOINで1クエリに
SELECT u.*, o.*
FROM users u
LEFT JOIN orders o ON u.id = o.user_id;

-- OK: サブクエリで集計
SELECT u.*, (
    SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id
) as order_count
FROM users u;

-- ORM使用時はEager Loading設定を確認
```

### よくある最適化パターン

```sql
-- NG: 関数でインデックスが効かない
SELECT * FROM users WHERE LOWER(email) = 'test@example.com';

-- OK: 関数インデックス or アプリ側で正規化
CREATE INDEX idx_users_email_lower ON users(LOWER(email));

-- NG: OR条件でインデックスが効きにくい
SELECT * FROM orders WHERE status = 'pending' OR status = 'processing';

-- OK: INに書き換え
SELECT * FROM orders WHERE status IN ('pending', 'processing');

-- NG: LIKE前方一致以外
SELECT * FROM users WHERE name LIKE '%田中%';

-- OK: 全文検索を使用（PostgreSQL）
CREATE INDEX idx_users_name_gin ON users USING gin(name gin_trgm_ops);
SELECT * FROM users WHERE name LIKE '%田中%';
```

## トランザクション設計

### 分離レベル

```sql
-- PostgreSQL
BEGIN;
SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;
-- 処理
COMMIT;
```

| レベル | Dirty Read | Non-repeatable Read | Phantom Read |
|--------|------------|---------------------|--------------|
| READ UNCOMMITTED | あり | あり | あり |
| READ COMMITTED | なし | あり | あり |
| REPEATABLE READ | なし | なし | あり(MySQL)/なし(PG) |
| SERIALIZABLE | なし | なし | なし |

### デッドロック回避

```sql
-- 常に同じ順序でロックを取得
BEGIN;
SELECT * FROM accounts WHERE id = 1 FOR UPDATE;
SELECT * FROM accounts WHERE id = 2 FOR UPDATE;
-- 処理
COMMIT;

-- タイムアウト設定
SET lock_timeout = '5s';
```

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
- マイグレーションがロールバック可能か
- 本番データ量でのパフォーマンステスト
- migration dry-run、テストDBへの適用、`EXPLAIN` / `EXPLAIN ANALYZE` の確認ができたか
- 最終報告に変更内容、互換性影響、lock/rollback の見通し、実行した検証、未検証リスクを含める

## リファレンス

詳細なガイドは以下を参照:

- **正規化とモデリング**: [references/normalization.md](references/normalization.md)
- **クエリ最適化詳細**: [references/query-optimization.md](references/query-optimization.md)
- **安全なマイグレーション実例**: [references/migrations.md](references/migrations.md)
- **各DBエンジン固有のTips（監視SQL、MongoDB実例を含む）**: [references/engine-specific.md](references/engine-specific.md)
