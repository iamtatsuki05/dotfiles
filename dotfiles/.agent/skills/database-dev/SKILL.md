---
name: database-dev
description: データベース設計・開発のための汎用スキル。スキーマ設計、クエリ最適化、インデックス設計、マイグレーション作成、パフォーマンスチューニングを支援。SQL/NoSQL（PostgreSQL、MySQL、SQLite、MongoDB等）のテーブル/コレクション設計、正規化、ER図作成、クエリ分析、EXPLAIN解析、N+1問題検出、トランザクション設計時に使用。
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

-- 更新日時の自動更新（PostgreSQL）
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

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

### 安全なマイグレーション

```sql
-- カラム追加（デフォルト値なし = 即座に完了）
ALTER TABLE users ADD COLUMN phone VARCHAR(20);

-- カラム追加（デフォルト値あり）
-- PostgreSQL 11+: 即座に完了
-- それ以前/MySQL: 全行書き換え発生
ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT false;

-- 大きなテーブルへのインデックス追加
CREATE INDEX CONCURRENTLY idx_orders_status ON orders(status);

-- カラム削除は段階的に
-- 1. アプリからの参照を削除
-- 2. NOT NULL制約を外す
-- 3. しばらく経ってからカラム削除
ALTER TABLE users ALTER COLUMN old_column DROP NOT NULL;
ALTER TABLE users DROP COLUMN old_column;
```

### ダウンタイムなしの変更

```sql
-- カラム名変更（段階的）
-- 1. 新カラム追加
ALTER TABLE users ADD COLUMN display_name VARCHAR(100);
-- 2. データコピー（バッチ処理）
UPDATE users SET display_name = name WHERE display_name IS NULL LIMIT 1000;
-- 3. アプリで両方参照
-- 4. 旧カラム削除
```

## NoSQL (MongoDB) パターン

### ドキュメント設計

```javascript
// 埋め込み（1対少、頻繁にアクセス）
{
  _id: ObjectId("..."),
  name: "John",
  addresses: [
    { type: "home", city: "Tokyo" },
    { type: "work", city: "Osaka" }
  ]
}

// 参照（1対多、独立してアクセス）
// users collection
{ _id: ObjectId("user1"), name: "John" }

// orders collection
{ _id: ObjectId("order1"), user_id: ObjectId("user1"), total: 1000 }
```

### インデックス

```javascript
// 単一フィールド
db.users.createIndex({ email: 1 }, { unique: true });

// 複合インデックス
db.orders.createIndex({ user_id: 1, created_at: -1 });

// 部分インデックス
db.orders.createIndex(
  { created_at: 1 },
  { partialFilterExpression: { status: "pending" } }
);

// テキストインデックス
db.products.createIndex({ name: "text", description: "text" });
```

## パフォーマンス監視

### PostgreSQL

```sql
-- スロークエリ確認
SELECT query, calls, mean_time, total_time
FROM pg_stat_statements
ORDER BY mean_time DESC
LIMIT 10;

-- テーブル統計
SELECT relname, seq_scan, idx_scan, n_live_tup
FROM pg_stat_user_tables
ORDER BY seq_scan DESC;

-- インデックス使用状況
SELECT indexrelname, idx_scan, idx_tup_read
FROM pg_stat_user_indexes
WHERE idx_scan = 0;  -- 未使用インデックス
```

### MySQL

```sql
-- スロークエリログ有効化
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 1;

-- クエリ実行計画キャッシュ
SHOW STATUS LIKE 'Qcache%';
```

## コード品質チェック

実装後に確認:
- スキーマに適切な制約（NOT NULL, UNIQUE, FK, CHECK）があるか
- インデックスが適切に設定されているか
- N+1クエリが発生していないか
- マイグレーションがロールバック可能か
- 本番データ量でのパフォーマンステスト

## リファレンス

詳細なガイドは以下を参照:

- **正規化とモデリング**: [references/normalization.md](references/normalization.md)
- **クエリ最適化詳細**: [references/query-optimization.md](references/query-optimization.md)
- **各DBエンジン固有のTips**: [references/engine-specific.md](references/engine-specific.md)
