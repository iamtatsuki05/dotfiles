# データベース正規化とモデリング

## 正規化の詳細

### 非正規形から第3正規形への変換例

```
非正規形:
┌─────────────────────────────────────────────────────────────┐
│ 注文ID | 顧客名 | 商品1,商品2,商品3 | 合計金額              │
└─────────────────────────────────────────────────────────────┘

第1正規形 (1NF) - 繰り返しグループを排除:
┌───────────────────────────────────────────────┐
│ 注文ID | 顧客名 | 商品名 | 数量 | 単価        │
├───────────────────────────────────────────────┤
│ 001    | 田中   | りんご | 2    | 100         │
│ 001    | 田中   | みかん | 3    | 80          │
└───────────────────────────────────────────────┘

第2正規形 (2NF) - 部分関数従属を排除:
Orders: 注文ID, 顧客名
OrderItems: 注文ID, 商品名, 数量, 単価

第3正規形 (3NF) - 推移的関数従属を排除:
Orders: 注文ID, 顧客ID
Customers: 顧客ID, 顧客名
Products: 商品ID, 商品名, 単価
OrderItems: 注文ID, 商品ID, 数量
```

### 関数従属性の判定

```
完全関数従属: X → Y で、Xの真部分集合からYが決まらない
部分関数従属: 複合キー(A,B) → C で、A → C または B → C が成立
推移的関数従属: X → Y → Z（XからYを経由してZが決まる）
```

## ER図の表記法

### IE記法（カラス足記法）

```
1対1:     A ──────── B
1対多:    A ──────<  B  (Aが1、Bが多)
多対多:   A >──────< B  (中間テーブル必要)

オプショナル: ○ (0または1)
必須:        | (1)

例: ユーザーは複数の注文を持てる（0以上）
User ||──────o< Order
```

### 論理モデル → 物理モデル

```
論理モデル:
┌──────────────┐       ┌──────────────┐
│   ユーザー    │       │    注文      │
├──────────────┤       ├──────────────┤
│ ユーザーID   │───────│ 注文ID       │
│ 名前         │       │ ユーザーID   │
│ メール       │       │ 注文日       │
└──────────────┘       └──────────────┘

物理モデル (PostgreSQL):
┌──────────────────────┐       ┌──────────────────────┐
│ users                │       │ orders               │
├──────────────────────┤       ├──────────────────────┤
│ id: UUID PK          │───────│ id: UUID PK          │
│ name: VARCHAR(100)   │       │ user_id: UUID FK     │
│ email: VARCHAR(255)  │       │ ordered_at: TIMESTAMP│
│ created_at: TIMESTAMP│       │ created_at: TIMESTAMP│
└──────────────────────┘       └──────────────────────┘
```

## 非正規化のパターン

### 意図的な非正規化

```sql
-- 集計値のキャッシュ
CREATE TABLE users (
    id UUID PRIMARY KEY,
    name VARCHAR(100),
    order_count INTEGER DEFAULT 0,  -- 非正規化: 注文数をキャッシュ
    total_spent DECIMAL(10,2) DEFAULT 0  -- 非正規化: 累計金額
);

-- トリガーで同期
CREATE OR REPLACE FUNCTION update_user_stats()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE users SET
            order_count = order_count + 1,
            total_spent = total_spent + NEW.total_amount
        WHERE id = NEW.user_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE users SET
            order_count = order_count - 1,
            total_spent = total_spent - OLD.total_amount
        WHERE id = OLD.user_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
```

### マテリアライズドビュー

```sql
-- 重い集計をマテリアライズドビューに
CREATE MATERIALIZED VIEW monthly_sales AS
SELECT
    DATE_TRUNC('month', created_at) as month,
    SUM(total_amount) as revenue,
    COUNT(*) as order_count
FROM orders
GROUP BY DATE_TRUNC('month', created_at);

-- 定期的に更新
REFRESH MATERIALIZED VIEW CONCURRENTLY monthly_sales;
```

## 階層データのモデリング

### 隣接リスト（Adjacency List）

```sql
CREATE TABLE categories (
    id UUID PRIMARY KEY,
    name VARCHAR(100),
    parent_id UUID REFERENCES categories(id)
);

-- 欠点: 深い階層の取得にN回クエリが必要
```

### 経路列挙（Path Enumeration）

```sql
CREATE TABLE categories (
    id UUID PRIMARY KEY,
    name VARCHAR(100),
    path TEXT  -- '/1/4/7/' のような形式
);

-- 全子孫取得
SELECT * FROM categories WHERE path LIKE '/1/4/%';
```

### 入れ子集合（Nested Set）

```sql
CREATE TABLE categories (
    id UUID PRIMARY KEY,
    name VARCHAR(100),
    lft INTEGER,
    rgt INTEGER
);

-- 全子孫取得（1クエリ）
SELECT * FROM categories
WHERE lft > parent.lft AND rgt < parent.rgt;

-- 欠点: 挿入・削除時に多くの行を更新
```

### クロージャテーブル（Closure Table）

```sql
CREATE TABLE categories (
    id UUID PRIMARY KEY,
    name VARCHAR(100)
);

CREATE TABLE category_closure (
    ancestor_id UUID REFERENCES categories(id),
    descendant_id UUID REFERENCES categories(id),
    depth INTEGER,
    PRIMARY KEY (ancestor_id, descendant_id)
);

-- 全子孫取得
SELECT c.* FROM categories c
JOIN category_closure cc ON c.id = cc.descendant_id
WHERE cc.ancestor_id = ?;

-- 直接の子のみ
SELECT c.* FROM categories c
JOIN category_closure cc ON c.id = cc.descendant_id
WHERE cc.ancestor_id = ? AND cc.depth = 1;
```

## 時系列データのモデリング

### タイムスタンプベース

```sql
-- 単純なログテーブル
CREATE TABLE event_logs (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(50),
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- パーティショニング（PostgreSQL 10+）
CREATE TABLE metrics (
    id BIGSERIAL,
    metric_name VARCHAR(100),
    value DECIMAL(10,4),
    recorded_at TIMESTAMPTZ
) PARTITION BY RANGE (recorded_at);

CREATE TABLE metrics_2024_01 PARTITION OF metrics
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
```

### イベントソーシング

```sql
CREATE TABLE events (
    id UUID PRIMARY KEY,
    aggregate_type VARCHAR(50),
    aggregate_id UUID,
    event_type VARCHAR(100),
    event_data JSONB,
    version INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (aggregate_id, version)
);

-- スナップショット
CREATE TABLE snapshots (
    aggregate_id UUID PRIMARY KEY,
    aggregate_type VARCHAR(50),
    state JSONB,
    version INTEGER,
    created_at TIMESTAMPTZ
);
```

## ソフトデリート vs ハードデリート

```sql
-- ソフトデリート
CREATE TABLE users (
    id UUID PRIMARY KEY,
    email VARCHAR(255),
    deleted_at TIMESTAMPTZ  -- NULLなら有効
);

CREATE INDEX idx_users_active ON users(email) WHERE deleted_at IS NULL;

-- アプリ側で常にフィルタ
SELECT * FROM users WHERE deleted_at IS NULL;

-- ハードデリート + 監査ログ
CREATE TABLE users_audit (
    id UUID,
    email VARCHAR(255),
    operation VARCHAR(10),  -- INSERT, UPDATE, DELETE
    changed_at TIMESTAMPTZ,
    changed_by UUID
);
```
