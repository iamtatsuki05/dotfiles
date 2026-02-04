# クエリ最適化詳細ガイド

## EXPLAINの読み方

### PostgreSQL EXPLAIN出力

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT * FROM orders WHERE user_id = '...' AND status = 'pending';

-- 出力例:
Index Scan using idx_orders_user_status on orders  (cost=0.43..8.45 rows=1 width=100) (actual time=0.025..0.027 rows=1 loops=1)
  Index Cond: ((user_id = '...'::uuid) AND (status = 'pending'))
  Buffers: shared hit=4
Planning Time: 0.150 ms
Execution Time: 0.050 ms
```

### 主要なノードタイプ

| ノード | 説明 | 注意点 |
|--------|------|--------|
| Seq Scan | 全件スキャン | 大テーブルでは要改善 |
| Index Scan | インデックス使用 | 効率的 |
| Index Only Scan | インデックスのみ | 最も効率的 |
| Bitmap Index Scan | 複数条件の合成 | 中間的な効率 |
| Nested Loop | ネストループ結合 | 内側が小さければOK |
| Hash Join | ハッシュ結合 | 等価結合で効率的 |
| Merge Join | マージ結合 | ソート済みで効率的 |
| Sort | ソート処理 | work_mem超過に注意 |

### コスト値の解釈

```
cost=startup..total
- startup: 最初の行を返すまでのコスト
- total: 全行を返すまでのコスト
- rows: 推定行数
- width: 1行あたりのバイト数

actual time=first..last
- first: 最初の行を返した時間(ms)
- last: 全行を返した時間(ms)
- rows: 実際の行数
- loops: 実行回数
```

## インデックス詳細

### B-treeインデックス

```sql
-- デフォルトのインデックスタイプ
-- 等価、範囲、ソートに有効
CREATE INDEX idx_orders_date ON orders(created_at);

-- 複合インデックスの順序
-- (a, b, c) の場合:
-- ○ WHERE a = ?
-- ○ WHERE a = ? AND b = ?
-- ○ WHERE a = ? AND b = ? AND c = ?
-- × WHERE b = ?
-- × WHERE c = ?
-- △ WHERE a = ? AND c = ?  (aのみ使用)
```

### GINインデックス

```sql
-- 全文検索
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_products_name_gin ON products USING gin(name gin_trgm_ops);

-- JSONBフィールド
CREATE INDEX idx_data_gin ON documents USING gin(data);
SELECT * FROM documents WHERE data @> '{"status": "active"}';

-- 配列
CREATE INDEX idx_tags_gin ON articles USING gin(tags);
SELECT * FROM articles WHERE tags @> ARRAY['tech', 'ai'];
```

### GiSTインデックス

```sql
-- 地理空間データ
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE INDEX idx_locations_gist ON stores USING gist(location);

-- 範囲型
CREATE INDEX idx_reservations_gist ON reservations USING gist(during);
SELECT * FROM reservations WHERE during && '[2024-01-01, 2024-01-31]'::daterange;
```

### BRINインデックス

```sql
-- 時系列データに有効（物理的にソートされている場合）
CREATE INDEX idx_logs_created_brin ON logs USING brin(created_at);

-- 非常に小さいサイズで高速な範囲検索
-- ただしランダムアクセスには不向き
```

## ジョインの最適化

### ジョインアルゴリズムの選択

```sql
-- Nested Loop（小さいテーブル同士）
SET enable_hashjoin = off;
SET enable_mergejoin = off;
EXPLAIN SELECT * FROM small_a JOIN small_b ON a.id = b.a_id;

-- Hash Join（等価結合、片方が小さい）
SET enable_nestloop = off;
SET enable_mergejoin = off;
EXPLAIN SELECT * FROM big_a JOIN small_b ON a.id = b.a_id;

-- Merge Join（両方ソート済み or ソートコストが低い）
SET enable_nestloop = off;
SET enable_hashjoin = off;
EXPLAIN SELECT * FROM sorted_a JOIN sorted_b ON a.id = b.a_id;
```

### ジョイン順序の制御

```sql
-- 統計情報の更新
ANALYZE orders;
ANALYZE users;

-- ジョイン順序のヒント（pg_hint_plan使用時）
/*+ Leading(users orders) */
SELECT * FROM users u JOIN orders o ON u.id = o.user_id;
```

## サブクエリの最適化

### 相関サブクエリの書き換え

```sql
-- NG: 相関サブクエリ（行ごとに実行）
SELECT *
FROM users u
WHERE EXISTS (
    SELECT 1 FROM orders o
    WHERE o.user_id = u.id AND o.created_at > '2024-01-01'
);

-- OK: JOINに書き換え
SELECT DISTINCT u.*
FROM users u
JOIN orders o ON u.id = o.user_id
WHERE o.created_at > '2024-01-01';

-- OK: 半結合（PostgreSQLは自動でEXISTSを最適化することが多い）
```

### IN vs EXISTS vs JOIN

```sql
-- IN（リストが小さい場合）
SELECT * FROM users WHERE id IN (SELECT user_id FROM premium_users);

-- EXISTS（大きなテーブルで一致確認）
SELECT * FROM users u
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id);

-- JOIN（結合後の処理が必要な場合）
SELECT u.*, COUNT(o.id) FROM users u
LEFT JOIN orders o ON u.id = o.user_id
GROUP BY u.id;
```

## ページネーション最適化

### OFFSET問題

```sql
-- NG: OFFSETは深いページで遅い
SELECT * FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 10000;

-- OK: カーソルベースのページネーション
SELECT * FROM orders
WHERE created_at < '2024-01-15 10:30:00'
ORDER BY created_at DESC
LIMIT 20;

-- OK: キーセットページネーション（複合キー）
SELECT * FROM orders
WHERE (created_at, id) < ('2024-01-15 10:30:00', 'uuid-xxx')
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

### 総件数の取得

```sql
-- NG: 毎回COUNT(*)
SELECT COUNT(*) FROM orders WHERE status = 'pending';
SELECT * FROM orders WHERE status = 'pending' LIMIT 20;

-- OK: 概算値を使用
SELECT reltuples::bigint FROM pg_class WHERE relname = 'orders';

-- OK: 別テーブルでカウントを管理
CREATE TABLE order_counts (
    status VARCHAR(20) PRIMARY KEY,
    count INTEGER DEFAULT 0
);
```

## ロック最適化

### ロックの種類（PostgreSQL）

```sql
-- 行レベルロック
SELECT * FROM accounts WHERE id = 1 FOR UPDATE;           -- 排他ロック
SELECT * FROM accounts WHERE id = 1 FOR NO KEY UPDATE;    -- キー以外の更新用
SELECT * FROM accounts WHERE id = 1 FOR SHARE;            -- 共有ロック
SELECT * FROM accounts WHERE id = 1 FOR KEY SHARE;        -- キーのみ共有

-- SKIP LOCKED（キューイング処理）
SELECT * FROM jobs
WHERE status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

### MVCC（Multi-Version Concurrency Control）

```sql
-- PostgreSQLではMVCCにより読み取りはロックされない
-- ただしバキュームが必要

-- テーブル肥大化の確認
SELECT pg_size_pretty(pg_total_relation_size('orders'));

-- 手動VACUUM
VACUUM (VERBOSE, ANALYZE) orders;

-- 自動VACUUM設定
ALTER TABLE orders SET (autovacuum_vacuum_scale_factor = 0.05);
```

## 統計情報

### 統計情報の確認

```sql
-- カラム統計
SELECT
    attname,
    n_distinct,
    most_common_vals,
    most_common_freqs
FROM pg_stats
WHERE tablename = 'orders' AND attname = 'status';

-- テーブル統計
SELECT
    relname,
    n_live_tup,
    n_dead_tup,
    last_vacuum,
    last_analyze
FROM pg_stat_user_tables
WHERE relname = 'orders';
```

### 統計情報の更新

```sql
-- 特定テーブルの統計を更新
ANALYZE orders;

-- 統計サンプル数の増加（精度向上）
ALTER TABLE orders ALTER COLUMN user_id SET STATISTICS 1000;
ANALYZE orders;
```

## パラレルクエリ

```sql
-- パラレルワーカー数の確認
SHOW max_parallel_workers_per_gather;

-- パラレル実行の確認
EXPLAIN SELECT COUNT(*) FROM large_table;
-- Gather (workers planned: 2)
--   -> Parallel Seq Scan on large_table

-- パラレル処理を強制
SET parallel_tuple_cost = 0;
SET parallel_setup_cost = 0;
SET min_parallel_table_scan_size = 0;
```
