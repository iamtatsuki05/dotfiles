# OpenAPI仕様詳細リファレンス

## OpenAPI 3.1.0 構造

### ルートオブジェクト

```yaml
openapi: 3.1.0  # 必須
info: {}        # 必須
servers: []
paths: {}       # 必須（または webhooks）
webhooks: {}
components: {}
security: []
tags: []
externalDocs: {}
```

### Info オブジェクト

```yaml
info:
  title: My API          # 必須
  version: 1.0.0         # 必須
  summary: 簡単な要約
  description: |
    マークダウン形式の詳細説明
    - 機能1
    - 機能2
  termsOfService: https://example.com/terms
  contact:
    name: API Support
    url: https://example.com/support
    email: support@example.com
  license:
    name: MIT
    identifier: MIT
```

### Server オブジェクト

```yaml
servers:
  - url: https://api.example.com/{version}
    description: Production server
    variables:
      version:
        default: v1
        enum: [v1, v2]
        description: API version
  - url: https://api-staging.example.com/v1
    description: Staging server
```

---

## パスとオペレーション

### Path Item オブジェクト

```yaml
paths:
  /users/{userId}:
    summary: ユーザー操作
    description: ユーザーに関する操作
    parameters:
      - $ref: '#/components/parameters/UserId'
    get:
      # Operation Object
    put:
      # Operation Object
    delete:
      # Operation Object
```

### Operation オブジェクト

```yaml
get:
  tags:
    - users
  summary: ユーザー取得
  description: 指定されたIDのユーザーを取得
  operationId: getUser
  externalDocs:
    url: https://docs.example.com/users
  parameters: []
  requestBody: {}
  responses: {}
  callbacks: {}
  deprecated: false
  security: []
  servers: []
```

### Parameter オブジェクト

```yaml
parameters:
  - name: userId
    in: path           # path, query, header, cookie
    description: ユーザーID
    required: true     # pathの場合は必須
    deprecated: false
    allowEmptyValue: false
    style: simple      # matrix, label, form, simple, spaceDelimited, pipeDelimited, deepObject
    explode: false
    schema:
      type: string
      format: uuid
    example: 550e8400-e29b-41d4-a716-446655440000

  # クエリパラメータ例
  - name: filter
    in: query
    style: deepObject
    explode: true
    schema:
      type: object
      properties:
        status:
          type: string
        createdAfter:
          type: string
          format: date
```

### RequestBody オブジェクト

```yaml
requestBody:
  description: ユーザー作成リクエスト
  required: true
  content:
    application/json:
      schema:
        $ref: '#/components/schemas/CreateUserRequest'
      example:
        email: user@example.com
        name: John Doe
      examples:
        basic:
          summary: 基本的なリクエスト
          value:
            email: user@example.com
        full:
          summary: 全フィールド指定
          value:
            email: user@example.com
            name: John Doe
    multipart/form-data:
      schema:
        type: object
        properties:
          file:
            type: string
            format: binary
          metadata:
            type: string
```

### Response オブジェクト

```yaml
responses:
  '200':
    description: 成功
    headers:
      X-RateLimit-Remaining:
        description: 残りリクエスト数
        schema:
          type: integer
    content:
      application/json:
        schema:
          $ref: '#/components/schemas/User'
    links:
      GetUserOrders:
        operationId: getUserOrders
        parameters:
          userId: $response.body#/id
  '201':
    description: 作成成功
    headers:
      Location:
        description: 作成されたリソースのURL
        schema:
          type: string
          format: uri
  '204':
    description: 成功（レスポンスなし）
  '4XX':
    $ref: '#/components/responses/ClientError'
  default:
    $ref: '#/components/responses/UnexpectedError'
```

---

## スキーマ定義

### 基本型

```yaml
schemas:
  # 文字列型
  Email:
    type: string
    format: email
    maxLength: 254

  # 数値型
  Price:
    type: number
    format: double
    minimum: 0
    exclusiveMinimum: true
    multipleOf: 0.01

  # 整数型
  Age:
    type: integer
    minimum: 0
    maximum: 150

  # 真偽値
  IsActive:
    type: boolean
    default: true

  # 配列型
  Tags:
    type: array
    items:
      type: string
    minItems: 1
    maxItems: 10
    uniqueItems: true

  # Null許容
  NullableString:
    type:
      - string
      - 'null'
```

### 複合型

```yaml
schemas:
  # oneOf（いずれか1つ）
  Pet:
    oneOf:
      - $ref: '#/components/schemas/Dog'
      - $ref: '#/components/schemas/Cat'
    discriminator:
      propertyName: petType
      mapping:
        dog: '#/components/schemas/Dog'
        cat: '#/components/schemas/Cat'

  # anyOf（1つ以上）
  PetOrOwner:
    anyOf:
      - $ref: '#/components/schemas/Pet'
      - $ref: '#/components/schemas/Owner'

  # allOf（すべて）
  CreateUserResponse:
    allOf:
      - $ref: '#/components/schemas/User'
      - type: object
        properties:
          token:
            type: string
```

### 高度なスキーマ

```yaml
schemas:
  # 追加プロパティ
  Metadata:
    type: object
    additionalProperties:
      type: string
    example:
      key1: value1
      key2: value2

  # パターンプロパティ
  DynamicObject:
    type: object
    patternProperties:
      '^x-':
        type: string

  # 条件付きスキーマ
  ConditionalObject:
    type: object
    properties:
      type:
        type: string
        enum: [personal, business]
      companyName:
        type: string
    if:
      properties:
        type:
          const: business
    then:
      required:
        - companyName

  # 定数
  FixedValue:
    const: fixed_value

  # 列挙型（詳細）
  Status:
    type: string
    enum:
      - active
      - inactive
      - pending
    description: |
      - `active`: アクティブ状態
      - `inactive`: 非アクティブ状態
      - `pending`: 保留状態
```

---

## セキュリティ定義

### セキュリティスキーム

```yaml
components:
  securitySchemes:
    # Basic認証
    basicAuth:
      type: http
      scheme: basic

    # Bearer Token
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

    # API Key
    apiKeyHeader:
      type: apiKey
      in: header
      name: X-API-Key

    apiKeyQuery:
      type: apiKey
      in: query
      name: api_key

    apiKeyCookie:
      type: apiKey
      in: cookie
      name: api_key

    # OAuth 2.0
    oauth2:
      type: oauth2
      flows:
        implicit:
          authorizationUrl: https://auth.example.com/authorize
          scopes:
            read: 読み取り権限
            write: 書き込み権限
        authorizationCode:
          authorizationUrl: https://auth.example.com/authorize
          tokenUrl: https://auth.example.com/token
          refreshUrl: https://auth.example.com/refresh
          scopes:
            read: 読み取り権限
            write: 書き込み権限
        clientCredentials:
          tokenUrl: https://auth.example.com/token
          scopes:
            admin: 管理者権限
        password:
          tokenUrl: https://auth.example.com/token
          scopes:
            read: 読み取り権限

    # OpenID Connect
    openIdConnect:
      type: openIdConnect
      openIdConnectUrl: https://auth.example.com/.well-known/openid-configuration

    # Mutual TLS
    mutualTLS:
      type: mutualTLS
```

### セキュリティ適用

```yaml
# グローバルセキュリティ
security:
  - bearerAuth: []
  - oauth2:
      - read
      - write

# オペレーション単位でオーバーライド
paths:
  /public:
    get:
      security: []  # 認証不要
  /admin:
    get:
      security:
        - bearerAuth: []
        - apiKeyHeader: []
```

---

## Webhooks

```yaml
webhooks:
  userCreated:
    post:
      summary: ユーザー作成通知
      operationId: userCreatedWebhook
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/UserCreatedEvent'
      responses:
        '200':
          description: Webhook受信確認
```

---

## 再利用可能コンポーネント

```yaml
components:
  schemas: {}
  responses: {}
  parameters: {}
  examples: {}
  requestBodies: {}
  headers: {}
  securitySchemes: {}
  links: {}
  callbacks: {}
  pathItems: {}
```

### 参照の使用

```yaml
# ローカル参照
$ref: '#/components/schemas/User'

# 外部ファイル参照
$ref: './common/schemas.yaml#/User'

# URL参照
$ref: 'https://api.example.com/schemas/user.yaml'
```
