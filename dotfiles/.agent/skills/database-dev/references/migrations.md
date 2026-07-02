# 安全なマイグレーション実例

判断基準は SKILL.md の「マイグレーション」節を参照。ここでは SQL 実例をまとめる。

## カラム追加

```sql
-- デフォルト値なし = 即座に完了
ALTER TABLE users ADD COLUMN phone VARCHAR(20);

-- デフォルト値あり
-- PostgreSQL 11+: 非 volatile なデフォルト（定数など）なら即座に完了
-- volatile なデフォルト（gen_random_uuid() 等）や PostgreSQL 10 以前: 全行書き換えが発生
-- MySQL: バージョンと ALGORITHM 指定で挙動が異なるため対象バージョンのドキュメントを確認
ALTER TABLE users ADD COLUMN is_verified BOOLEAN DEFAULT false;
```

## インデックス追加

```sql
-- 大きなテーブルには CONCURRENTLY（PostgreSQL）
-- 書き込みをブロックしないが、トランザクション内では実行できない
CREATE INDEX CONCURRENTLY idx_orders_status ON orders(status);
```

## カラム削除（段階的）

```sql
-- 1. アプリからの参照を削除
-- 2. NOT NULL制約を外す
-- 3. しばらく経ってからカラム削除
ALTER TABLE users ALTER COLUMN old_column DROP NOT NULL;
ALTER TABLE users DROP COLUMN old_column;
```

## カラム名変更（expand-contract）

直接の `RENAME COLUMN` はデプロイ中の新旧アプリと非互換になるため、段階的に行う。

```sql
-- 1. 新カラム追加
ALTER TABLE users ADD COLUMN display_name VARCHAR(100);

-- 2. データコピー（バッチ処理で分割。PostgreSQL は UPDATE に LIMIT を書けないため副問い合わせで分割する）
UPDATE users SET display_name = name
WHERE id IN (
    SELECT id FROM users WHERE display_name IS NULL LIMIT 1000
);

-- 3. アプリで両方に書き込み（二重書き込み）、読み取りを新カラムへ切替
-- 4. 旧カラム削除（上記「カラム削除」の手順で段階的に）
```
