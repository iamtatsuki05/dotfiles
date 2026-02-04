---
name: api-design
description: "OpenAPI/Swagger仕様書の作成とRESTful API設計を支援する汎用スキル。API仕様書(.yaml/.json)の作成・編集、エンドポイント設計、スキーマ定義、エラーレスポンス設計、バージョニング戦略を支援。「API設計」「OpenAPI作成」「Swagger仕様書」「REST API設計」「エンドポイント設計」「APIスキーマ定義」などのリクエスト時に使用。"
---

# API設計スキル

OpenAPI/Swagger仕様書の作成とRESTful API設計を効率的に行うためのガイド。

## ワークフロー判定

### 新規API設計
→ 「API設計ワークフロー」へ

### 既存OpenAPI仕様の編集
→ 「仕様書編集ワークフロー」へ

### API設計レビュー
→ 「レビューワークフロー」へ

---

## API設計ワークフロー

### Step 1: 要件確認

以下を確認する:
1. APIの目的と対象ドメイン
2. 対象クライアント（Web、モバイル、外部サービスなど）
3. 認証・認可方式（OAuth2、API Key、JWTなど）
4. バージョニング戦略（URL、ヘッダー、クエリパラメータ）

### Step 2: リソース設計

RESTfulリソースの命名規則:
- 名詞を使用（動詞は避ける）
- 複数形を使用: `/users`, `/orders`
- 階層関係を表現: `/users/{userId}/orders`
- ケバブケースを使用: `/user-profiles`

### Step 3: OpenAPI仕様書作成

基本構造:
```yaml
openapi: 3.1.0
info:
  title: API名
  version: 1.0.0
  description: APIの説明
servers:
  - url: https://api.example.com/v1
    description: Production
  - url: https://api-staging.example.com/v1
    description: Staging
paths:
  /resources:
    get:
      # ...
components:
  schemas:
    # ...
  securitySchemes:
    # ...
```

---

## RESTful設計原則

### HTTPメソッドの使い分け

| メソッド | 用途 | 冪等性 | 安全性 |
|---------|------|--------|--------|
| GET | リソース取得 | Yes | Yes |
| POST | リソース作成 | No | No |
| PUT | リソース全体更新 | Yes | No |
| PATCH | リソース部分更新 | No | No |
| DELETE | リソース削除 | Yes | No |

### エンドポイント設計例

```yaml
paths:
  /users:
    get:
      summary: ユーザー一覧取得
      operationId: listUsers
      parameters:
        - name: page
          in: query
          schema:
            type: integer
            default: 1
        - name: limit
          in: query
          schema:
            type: integer
            default: 20
            maximum: 100
      responses:
        '200':
          description: 成功
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/UserList'
    post:
      summary: ユーザー作成
      operationId: createUser
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUserRequest'
      responses:
        '201':
          description: 作成成功
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'

  /users/{userId}:
    parameters:
      - name: userId
        in: path
        required: true
        schema:
          type: string
          format: uuid
    get:
      summary: ユーザー詳細取得
      operationId: getUser
      responses:
        '200':
          description: 成功
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '404':
          $ref: '#/components/responses/NotFound'
    put:
      summary: ユーザー更新
      operationId: updateUser
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/UpdateUserRequest'
      responses:
        '200':
          description: 更新成功
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
    delete:
      summary: ユーザー削除
      operationId: deleteUser
      responses:
        '204':
          description: 削除成功
```

### スキーマ定義

```yaml
components:
  schemas:
    User:
      type: object
      required:
        - id
        - email
        - createdAt
      properties:
        id:
          type: string
          format: uuid
          readOnly: true
        email:
          type: string
          format: email
        name:
          type: string
          maxLength: 100
        status:
          type: string
          enum: [active, inactive, suspended]
          default: active
        createdAt:
          type: string
          format: date-time
          readOnly: true
        updatedAt:
          type: string
          format: date-time
          readOnly: true

    CreateUserRequest:
      type: object
      required:
        - email
      properties:
        email:
          type: string
          format: email
        name:
          type: string
          maxLength: 100

    UpdateUserRequest:
      type: object
      properties:
        email:
          type: string
          format: email
        name:
          type: string
          maxLength: 100
        status:
          type: string
          enum: [active, inactive, suspended]

    UserList:
      type: object
      properties:
        data:
          type: array
          items:
            $ref: '#/components/schemas/User'
        pagination:
          $ref: '#/components/schemas/Pagination'

    Pagination:
      type: object
      properties:
        page:
          type: integer
        limit:
          type: integer
        total:
          type: integer
        totalPages:
          type: integer
```

---

## エラーレスポンス設計

### 標準エラー形式

```yaml
components:
  schemas:
    Error:
      type: object
      required:
        - code
        - message
      properties:
        code:
          type: string
          description: エラーコード
        message:
          type: string
          description: エラーメッセージ
        details:
          type: array
          items:
            type: object
            properties:
              field:
                type: string
              message:
                type: string

  responses:
    BadRequest:
      description: リクエスト不正
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: VALIDATION_ERROR
            message: リクエストパラメータが不正です
            details:
              - field: email
                message: 有効なメールアドレスを入力してください

    Unauthorized:
      description: 認証エラー
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: UNAUTHORIZED
            message: 認証が必要です

    Forbidden:
      description: アクセス拒否
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: FORBIDDEN
            message: このリソースへのアクセス権限がありません

    NotFound:
      description: リソース未検出
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: NOT_FOUND
            message: 指定されたリソースが見つかりません

    Conflict:
      description: 競合エラー
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: CONFLICT
            message: リソースが既に存在します

    InternalServerError:
      description: サーバーエラー
      content:
        application/json:
          schema:
            $ref: '#/components/schemas/Error'
          example:
            code: INTERNAL_ERROR
            message: 内部エラーが発生しました
```

### HTTPステータスコード

| コード | 用途 |
|--------|------|
| 200 | 成功（GET, PUT, PATCH） |
| 201 | 作成成功（POST） |
| 204 | 成功、レスポンスなし（DELETE） |
| 400 | リクエスト不正 |
| 401 | 認証エラー |
| 403 | 認可エラー |
| 404 | リソース未検出 |
| 409 | 競合 |
| 422 | バリデーションエラー |
| 429 | レート制限 |
| 500 | サーバーエラー |

---

## 認証・認可

### OAuth 2.0 Bearer Token

```yaml
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

security:
  - bearerAuth: []
```

### API Key

```yaml
components:
  securitySchemes:
    apiKey:
      type: apiKey
      in: header
      name: X-API-Key
```

### OAuth 2.0 フロー

```yaml
components:
  securitySchemes:
    oauth2:
      type: oauth2
      flows:
        authorizationCode:
          authorizationUrl: https://auth.example.com/authorize
          tokenUrl: https://auth.example.com/token
          scopes:
            read:users: ユーザー情報の読み取り
            write:users: ユーザー情報の書き込み
```

---

## レビューワークフロー

### チェック項目

1. **リソース設計**
   - 適切な名詞が使用されているか
   - 階層関係が正しく表現されているか
   - 冗長なエンドポイントがないか

2. **HTTPメソッド**
   - 適切なメソッドが使用されているか
   - 冪等性が考慮されているか

3. **レスポンス**
   - 適切なステータスコードか
   - エラーレスポンスが統一されているか
   - ページネーションが実装されているか

4. **スキーマ**
   - 必須フィールドが明示されているか
   - 適切な型とフォーマットが使用されているか
   - バリデーション制約が定義されているか

5. **セキュリティ**
   - 認証方式が定義されているか
   - 適切なスコープが設定されているか

---

## リファレンス

詳細なガイドは以下を参照:

- **OpenAPI仕様詳細**: [references/openapi-spec.md](references/openapi-spec.md)
- **ベストプラクティス**: [references/best-practices.md](references/best-practices.md)
