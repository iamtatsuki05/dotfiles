# API設計ベストプラクティス

## リソース設計

### 命名規則

```
良い例:
  /users
  /user-profiles
  /orders/{orderId}/items

避けるべき例:
  /getUsers           # 動詞を使わない
  /user               # 単数形を使わない
  /userProfiles       # キャメルケースを使わない
  /users/getById/123  # 動詞を含めない
```

### 階層関係

```yaml
# リソースの所属関係を表現
/users/{userId}/orders                    # ユーザーの注文一覧
/users/{userId}/orders/{orderId}          # 特定の注文
/users/{userId}/orders/{orderId}/items    # 注文の商品一覧

# 独立したリソースとしてもアクセス可能に
/orders/{orderId}                         # 注文への直接アクセス
```

### アクション表現

RESTで表現しにくいアクションの場合:

```yaml
# POST + 動詞を使用（最終手段）
POST /users/{userId}/activate
POST /orders/{orderId}/cancel
POST /payments/{paymentId}/refund

# または状態変更としてPATCH
PATCH /users/{userId}
  { "status": "active" }

PATCH /orders/{orderId}
  { "status": "cancelled" }
```

---

## クエリパラメータ

### フィルタリング

```yaml
# シンプルなフィルタ
GET /users?status=active&role=admin

# 複合フィルタ
GET /orders?filter[status]=pending&filter[createdAfter]=2024-01-01

# 範囲フィルタ
GET /products?price[gte]=100&price[lte]=500
```

### ソート

```yaml
# 単一フィールド
GET /users?sort=createdAt
GET /users?sort=-createdAt  # 降順

# 複数フィールド
GET /users?sort=status,-createdAt

# 明示的な方向指定
GET /users?sortBy=createdAt&sortOrder=desc
```

### ページネーション

```yaml
# オフセットベース
GET /users?page=2&limit=20

# カーソルベース（大規模データ向け）
GET /users?cursor=eyJpZCI6MTAwfQ&limit=20

# レスポンス
{
  "data": [...],
  "pagination": {
    "page": 2,
    "limit": 20,
    "total": 150,
    "totalPages": 8,
    "hasNext": true,
    "hasPrev": true
  },
  "links": {
    "self": "/users?page=2&limit=20",
    "first": "/users?page=1&limit=20",
    "prev": "/users?page=1&limit=20",
    "next": "/users?page=3&limit=20",
    "last": "/users?page=8&limit=20"
  }
}
```

### フィールド選択

```yaml
# 必要なフィールドのみ取得
GET /users?fields=id,name,email

# ネストしたフィールド
GET /orders?fields=id,user.name,items.productId
```

### 関連リソースの展開

```yaml
# 関連リソースを含める
GET /orders?include=user,items

# レスポンス
{
  "data": {
    "id": "123",
    "userId": "456",
    "user": {
      "id": "456",
      "name": "John"
    },
    "items": [...]
  }
}
```

---

## バージョニング

### URL パスバージョニング（推奨）

```yaml
servers:
  - url: https://api.example.com/v1
  - url: https://api.example.com/v2
```

### ヘッダーバージョニング

```yaml
# リクエスト
GET /users HTTP/1.1
Accept: application/vnd.example.v2+json

# または
GET /users HTTP/1.1
X-API-Version: 2
```

### バージョン移行

```yaml
# 非推奨エンドポイント
/v1/users:
  get:
    deprecated: true
    x-deprecation-date: 2024-06-01
    x-sunset-date: 2024-12-01
    description: |
      **非推奨**: v2を使用してください。
      2024-12-01に廃止予定。

# 非推奨ヘッダー
responses:
  '200':
    headers:
      Deprecation:
        schema:
          type: string
        example: "true"
      Sunset:
        schema:
          type: string
          format: date-time
        example: "2024-12-01T00:00:00Z"
```

---

## エラーハンドリング

### 一貫したエラー形式

```yaml
schemas:
  Error:
    type: object
    required:
      - code
      - message
    properties:
      code:
        type: string
        description: |
          機械可読なエラーコード
          例: VALIDATION_ERROR, NOT_FOUND, RATE_LIMITED
      message:
        type: string
        description: 人間可読なエラーメッセージ
      target:
        type: string
        description: エラーが発生した対象（フィールド名など）
      details:
        type: array
        items:
          $ref: '#/components/schemas/ErrorDetail'
      traceId:
        type: string
        description: デバッグ用トレースID

  ErrorDetail:
    type: object
    properties:
      code:
        type: string
      message:
        type: string
      target:
        type: string
```

### バリデーションエラー

```json
{
  "code": "VALIDATION_ERROR",
  "message": "入力値が不正です",
  "details": [
    {
      "code": "REQUIRED",
      "message": "必須項目です",
      "target": "email"
    },
    {
      "code": "INVALID_FORMAT",
      "message": "有効なメールアドレスを入力してください",
      "target": "contactEmail"
    },
    {
      "code": "OUT_OF_RANGE",
      "message": "1以上100以下の値を入力してください",
      "target": "quantity"
    }
  ],
  "traceId": "abc123"
}
```

### ビジネスロジックエラー

```json
{
  "code": "INSUFFICIENT_BALANCE",
  "message": "残高が不足しています",
  "details": [
    {
      "code": "BALANCE_REQUIRED",
      "message": "必要な残高: ¥10,000, 現在の残高: ¥5,000"
    }
  ]
}
```

---

## レート制限

### ヘッダー

```yaml
responses:
  '200':
    headers:
      X-RateLimit-Limit:
        description: 期間内の最大リクエスト数
        schema:
          type: integer
        example: 1000
      X-RateLimit-Remaining:
        description: 残りリクエスト数
        schema:
          type: integer
        example: 999
      X-RateLimit-Reset:
        description: リセット時刻（Unix timestamp）
        schema:
          type: integer
        example: 1640000000
      Retry-After:
        description: 再試行までの秒数（429時）
        schema:
          type: integer
        example: 60
```

### 429レスポンス

```yaml
responses:
  '429':
    description: レート制限超過
    headers:
      Retry-After:
        schema:
          type: integer
    content:
      application/json:
        schema:
          $ref: '#/components/schemas/Error'
        example:
          code: RATE_LIMITED
          message: リクエスト制限を超過しました。60秒後に再試行してください。
```

---

## キャッシュ制御

### キャッシュヘッダー

```yaml
responses:
  '200':
    headers:
      Cache-Control:
        schema:
          type: string
        example: "max-age=3600, must-revalidate"
      ETag:
        schema:
          type: string
        example: '"abc123"'
      Last-Modified:
        schema:
          type: string
          format: date-time
```

### 条件付きリクエスト

```yaml
parameters:
  - name: If-None-Match
    in: header
    description: ETag値
    schema:
      type: string
  - name: If-Modified-Since
    in: header
    description: 最終更新日時
    schema:
      type: string
      format: date-time

responses:
  '304':
    description: 変更なし
```

---

## セキュリティ

### 入力バリデーション

```yaml
# 文字列長制限
name:
  type: string
  minLength: 1
  maxLength: 100

# パターン制限
username:
  type: string
  pattern: '^[a-zA-Z0-9_]{3,20}$'

# 配列サイズ制限
tags:
  type: array
  maxItems: 10
  items:
    type: string
    maxLength: 50
```

### センシティブデータ

```yaml
# パスワードなど機密データ
password:
  type: string
  format: password
  writeOnly: true  # レスポンスに含めない

# 内部用フィールド
internalId:
  type: string
  readOnly: true
  x-internal: true  # 拡張フィールドでマーク
```

### CORSヘッダー

```yaml
responses:
  '200':
    headers:
      Access-Control-Allow-Origin:
        schema:
          type: string
        example: "https://example.com"
      Access-Control-Allow-Methods:
        schema:
          type: string
        example: "GET, POST, PUT, DELETE"
      Access-Control-Allow-Headers:
        schema:
          type: string
        example: "Content-Type, Authorization"
```

---

## ドキュメント品質

### operationId

```yaml
# 一貫した命名規則
operationId: listUsers      # GET /users
operationId: createUser     # POST /users
operationId: getUser        # GET /users/{id}
operationId: updateUser     # PUT /users/{id}
operationId: deleteUser     # DELETE /users/{id}
operationId: getUserOrders  # GET /users/{id}/orders
```

### サンプル値

```yaml
schemas:
  User:
    type: object
    properties:
      id:
        type: string
        format: uuid
        example: "550e8400-e29b-41d4-a716-446655440000"
      email:
        type: string
        format: email
        example: "user@example.com"
      createdAt:
        type: string
        format: date-time
        example: "2024-01-15T09:30:00Z"
```

### タグによる整理

```yaml
tags:
  - name: users
    description: ユーザー管理
    externalDocs:
      url: https://docs.example.com/users
  - name: orders
    description: 注文管理
  - name: admin
    description: 管理者向けAPI

paths:
  /users:
    get:
      tags:
        - users
```
