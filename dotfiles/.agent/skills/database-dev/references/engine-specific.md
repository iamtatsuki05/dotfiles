# DBエンジン固有のTips

## 目次

- [PostgreSQL](#postgresql)
- [MySQL](#mysql)
- [SQLite](#sqlite)
- [MongoDB](#mongodb)
- [Redis（キャッシュ/セッション）](#redisキャッシュセッション)

## PostgreSQL

### 拡張機能

```sql
-- 利用可能な拡張
SELECT * FROM pg_available_extensions;

-- よく使う拡張
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";      -- UUID生成
CREATE EXTENSION IF NOT EXISTS "pgcrypto";       -- 暗号化
CREATE EXTENSION IF NOT EXISTS "pg_trgm";        -- 類似検索
CREATE EXTENSION IF NOT EXISTS "btree_gin";      -- GINインデックス拡張
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";  -- クエリ統計
```

### JSONBの活用

```sql
-- JSONB操作
SELECT data->'user'->>'name' FROM documents;           -- テキスト取得
SELECT data->'tags' FROM documents;                    -- JSON取得
SELECT * FROM documents WHERE data @> '{"active": true}';  -- 包含検索
SELECT * FROM documents WHERE data ? 'email';          -- キー存在確認
SELECT * FROM documents WHERE data ?| array['a', 'b']; -- いずれかのキー

-- JSONB更新
UPDATE documents SET data = jsonb_set(data, '{status}', '"updated"');
UPDATE documents SET data = data || '{"new_field": "value"}';
UPDATE documents SET data = data - 'old_field';

-- JSONB集計
SELECT jsonb_agg(name) FROM users WHERE active = true;
SELECT jsonb_object_agg(id, name) FROM categories;
```

### CTE（Common Table Expressions）

```sql
-- 基本的なCTE
WITH active_users AS (
    SELECT * FROM users WHERE status = 'active'
)
SELECT * FROM active_users WHERE created_at > '2024-01-01';

-- 再帰CTE（階層データ）
WITH RECURSIVE category_tree AS (
    -- ベースケース
    SELECT id, name, parent_id, 1 as level
    FROM categories WHERE parent_id IS NULL
    UNION ALL
    -- 再帰ケース
    SELECT c.id, c.name, c.parent_id, ct.level + 1
    FROM categories c
    JOIN category_tree ct ON c.parent_id = ct.id
)
SELECT * FROM category_tree ORDER BY level, name;
```

### Window関数

```sql
-- ランキング
SELECT
    name,
    revenue,
    RANK() OVER (ORDER BY revenue DESC) as rank,
    DENSE_RANK() OVER (ORDER BY revenue DESC) as dense_rank,
    ROW_NUMBER() OVER (ORDER BY revenue DESC) as row_num
FROM sales;

-- 累計・移動平均
SELECT
    date,
    amount,
    SUM(amount) OVER (ORDER BY date) as running_total,
    AVG(amount) OVER (ORDER BY date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) as moving_avg_7d
FROM daily_sales;

-- パーティション内の計算
SELECT
    department,
    name,
    salary,
    AVG(salary) OVER (PARTITION BY department) as dept_avg,
    salary - AVG(salary) OVER (PARTITION BY department) as diff_from_avg
FROM employees;
```

### UPSERT

```sql
-- ON CONFLICT（PostgreSQL 9.5+）
INSERT INTO users (email, name)
VALUES ('test@example.com', 'Test User')
ON CONFLICT (email)
DO UPDATE SET name = EXCLUDED.name, updated_at = NOW();

-- 何もしない
INSERT INTO users (email, name)
VALUES ('test@example.com', 'Test User')
ON CONFLICT DO NOTHING;
```

### updated_at 自動更新トリガー

```sql
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

### パフォーマンス監視

```sql
-- スロークエリ確認（pg_stat_statements 拡張が必要）
-- PostgreSQL 13+ のカラム名。12 以前は mean_time / total_time
SELECT query, calls, mean_exec_time, total_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
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

## MySQL

### エンジン選択

```sql
-- InnoDB（デフォルト、トランザクション対応）
CREATE TABLE orders (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    total DECIMAL(10,2)
) ENGINE=InnoDB;

-- InnoDBの設定確認
SHOW VARIABLES LIKE 'innodb%';
```

### インデックスヒント

```sql
-- インデックス使用の強制
SELECT * FROM orders USE INDEX (idx_user_id) WHERE user_id = 1;
SELECT * FROM orders FORCE INDEX (idx_user_id) WHERE user_id = 1;
SELECT * FROM orders IGNORE INDEX (idx_user_id) WHERE user_id = 1;
```

### JSON操作（MySQL 5.7+）

```sql
-- JSON関数
SELECT JSON_EXTRACT(data, '$.user.name') FROM documents;
SELECT data->'$.user.name' FROM documents;          -- 省略形
SELECT data->>'$.user.name' FROM documents;         -- テキスト取得

-- JSON検索
SELECT * FROM documents
WHERE JSON_CONTAINS(data, '"admin"', '$.roles');

-- JSON更新
UPDATE documents SET data = JSON_SET(data, '$.status', 'updated');
UPDATE documents SET data = JSON_REMOVE(data, '$.old_field');
```

### パーティショニング

```sql
-- RANGEパーティショニング
CREATE TABLE logs (
    id BIGINT AUTO_INCREMENT,
    created_at DATETIME,
    message TEXT,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (YEAR(created_at)) (
    PARTITION p2023 VALUES LESS THAN (2024),
    PARTITION p2024 VALUES LESS THAN (2025),
    PARTITION pmax VALUES LESS THAN MAXVALUE
);

-- パーティション追加
ALTER TABLE logs ADD PARTITION (
    PARTITION p2025 VALUES LESS THAN (2026)
);
```

### レプリケーション対応

```sql
-- 読み取りを明示的にスレーブに向ける（アプリ側で制御）
-- SELECT文は読み取りレプリカへ
-- INSERT/UPDATE/DELETEはプライマリへ

-- レプリケーション遅延確認
SHOW SLAVE STATUS\G
```

### パフォーマンス監視

```sql
-- スロークエリログ有効化
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 1;

-- 実行統計（performance_schema）
-- クエリキャッシュ（Qcache）は MySQL 8.0 で削除済み
SELECT DIGEST_TEXT, COUNT_STAR, AVG_TIMER_WAIT
FROM performance_schema.events_statements_summary_by_digest
ORDER BY AVG_TIMER_WAIT DESC
LIMIT 10;
```

## SQLite

### 特徴と制限

```sql
-- 型は参考程度（動的型付け）
-- INTEGER PRIMARY KEYは自動でROWIDと同期

-- WALモード（並行読み取り向上）
PRAGMA journal_mode = WAL;

-- 外部キー制約を有効化（デフォルトOFF）
PRAGMA foreign_keys = ON;
```

### パフォーマンス設定

```sql
-- キャッシュサイズ（ページ数、デフォルト-2000）
PRAGMA cache_size = -64000;  -- 64MB

-- 同期モード（速度 vs 安全性）
PRAGMA synchronous = NORMAL;  -- FULL, NORMAL, OFF

-- メモリ一時テーブル
PRAGMA temp_store = MEMORY;
```

### バルクインサート最適化

```sql
-- トランザクションで囲む
BEGIN;
INSERT INTO items (name) VALUES ('item1');
INSERT INTO items (name) VALUES ('item2');
-- ... 多数のINSERT
COMMIT;

-- プリペアドステートメント使用（アプリ側）
```

## MongoDB

### ドキュメント設計（埋め込み vs 参照）

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

### インデックス戦略

```javascript
// 複合インデックス（左端から使用）
db.orders.createIndex({ user_id: 1, created_at: -1 });

// TTLインデックス（自動削除）
db.sessions.createIndex({ "lastAccess": 1 }, { expireAfterSeconds: 3600 });

// ユニークインデックス（スパース）
db.users.createIndex(
  { email: 1 },
  { unique: true, sparse: true }  // nullは無視
);

// 部分インデックス
db.orders.createIndex(
  { created_at: 1 },
  { partialFilterExpression: { status: "pending" } }
);

// テキストインデックス
db.products.createIndex({ name: "text", description: "text" });

// インデックス使用状況
db.orders.aggregate([{ $indexStats: {} }]);
```

### アグリゲーションパイプライン

```javascript
db.orders.aggregate([
  // フィルタリング（早めに）
  { $match: { status: "completed" } },

  // 日付でグループ化
  { $group: {
    _id: { $dateToString: { format: "%Y-%m", date: "$created_at" } },
    totalRevenue: { $sum: "$total" },
    orderCount: { $sum: 1 }
  }},

  // ソート
  { $sort: { _id: -1 } },

  // 件数制限
  { $limit: 12 }
]);
```

### トランザクション（MongoDB 4.0+）

```javascript
const session = db.getMongo().startSession();
session.startTransaction();

try {
  db.accounts.updateOne(
    { _id: "A" },
    { $inc: { balance: -100 } },
    { session }
  );
  db.accounts.updateOne(
    { _id: "B" },
    { $inc: { balance: 100 } },
    { session }
  );
  session.commitTransaction();
} catch (e) {
  session.abortTransaction();
  throw e;
} finally {
  session.endSession();
}
```

### Change Streams

```javascript
// リアルタイム変更監視
const changeStream = db.orders.watch([
  { $match: { "operationType": { $in: ["insert", "update"] } } }
]);

changeStream.on("change", (change) => {
  console.log("Change detected:", change);
});
```

## Redis（キャッシュ/セッション）

### データ構造選択

```redis
# String（単純なキャッシュ）
SET user:1:profile "{...}" EX 3600

# Hash（オブジェクト）
HSET user:1 name "John" email "john@example.com"
HGET user:1 name
HGETALL user:1

# List（キュー）
LPUSH queue:jobs "job1"
RPOP queue:jobs

# Set（ユニークな集合）
SADD user:1:tags "tech" "ai"
SMEMBERS user:1:tags

# Sorted Set（スコア付きランキング）
ZADD leaderboard 100 "user:1" 200 "user:2"
ZREVRANGE leaderboard 0 9 WITHSCORES
```

### パターン

```redis
# キャッシュアサイドパターン
# 1. キャッシュ確認 → 2. なければDBから取得 → 3. キャッシュに保存

# 分散ロック
SET lock:resource "owner" NX EX 30
# 処理後
DEL lock:resource

# レートリミッティング（スライディングウィンドウ）
# Luaスクリプトで原子的に実行
```
