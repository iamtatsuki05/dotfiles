---
name: api-design
description: "Use when the user asks to design, edit, validate, or review REST APIs, OpenAPI/Swagger specs, endpoints, schemas, error responses, authentication, authorization, or versioning strategy."
---

# API設計スキル

OpenAPI/Swagger仕様書の作成とRESTful API設計を効率的に行うためのガイド。新規設計は「API設計ワークフロー」、既存仕様の変更は「既存OpenAPI仕様書の編集ワークフロー」、レビュー依頼は「レビューワークフロー」から始める。

## API設計ワークフロー

### Step 1: 要件確認

以下を確認する:
1. APIの目的と対象ドメイン
2. 対象クライアント（Web、モバイル、外部サービスなど）
3. 認証・認可方式（OAuth2、API Key、JWTなど）
4. バージョニング戦略（URL、ヘッダー、クエリパラメータ）

### Step 2: リソース設計

リソース命名は「名詞・複数形・ケバブケース」（`/users`, `/user-profiles`）を基本とし、所属関係は `/users/{userId}/orders` のような階層で表現する。RESTで表現しにくいアクションの扱いを含む詳細は [references/best-practices.md](references/best-practices.md) の「リソース設計」を参照。

### Step 3: OpenAPI仕様書作成

最小の骨組み（info / 1 endpoint / 1 schema）:

```yaml
openapi: 3.1.0
info:
  title: User API
  version: 1.0.0
  description: APIの説明
servers:
  - url: https://api.example.com/v1
    description: Production
paths:
  /users/{userId}:
    get:
      summary: ユーザー詳細取得
      operationId: getUser
      parameters:
        - name: userId
          in: path
          required: true
          schema:
            type: string
            format: uuid
      responses:
        '200':
          description: 成功
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/User'
        '404':
          description: リソース未検出
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
        createdAt:
          type: string
          format: date-time
          readOnly: true
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
security:
  - bearerAuth: []
```

CRUD 一式（一覧・作成・更新・削除、ページネーション、リクエスト/レスポンス schema）の実例は [references/openapi-spec.md](references/openapi-spec.md) の「実例: ユーザーリソースのCRUD」を参照。

## 既存OpenAPI仕様書の編集ワークフロー

1. 既存仕様の `openapi` バージョン、分割構成、共通 schema、security scheme、既存命名規則を確認する。
2. 変更対象の endpoint / schema / response を特定し、不要な再整形や無関係な並び替えを避ける。
3. 破壊的変更（パス削除、必須フィールド追加、型変更、レスポンス形式変更、認証スコープ変更）がある場合は、互換性への影響を明示してユーザー確認を取る。
4. 既存スタイルに合わせて最小差分で編集する。
5. プロジェクトで使われている検証コマンドがあればそれを優先し、無ければ `redocly lint`（`@redocly/cli`）を第一候補として実行する。旧来の `swagger-cli validate` は開発が停滞しているため新規には採用せず、既存プロジェクトで使われている場合のみ併用する。実行できなければ理由を報告する。
6. 最終報告には変更した endpoint / schema、互換性影響、実行した検証、未検証リスクを含める。

## エラーレスポンス設計

エラー schema は `components.schemas` に共通定義し、エラーの種類（400/401/403/404/409/500）ごとに `components.responses` を用意して全 endpoint で `$ref` により統一する。個別 endpoint にインラインでエラー形式を書かない。定義一式は [references/best-practices.md](references/best-practices.md) の「標準エラーレスポンス定義一式」を参照。

HTTPメソッドとステータスコードは標準的な使い分けに従い、プロジェクトで使う範囲（422 vs 400、429 の扱いなど）は既存仕様と [references/best-practices.md](references/best-practices.md) で確認して揃える。

## 認証・認可

Bearer Token（JWT）の定義は上記の骨組み例を参照。API Key の場合:

```yaml
components:
  securitySchemes:
    apiKey:
      type: apiKey
      in: header
      name: X-API-Key
```

OAuth 2.0 の各フロー、OpenID Connect、Mutual TLS、オペレーション単位のセキュリティ上書きは [references/openapi-spec.md](references/openapi-spec.md) の「セキュリティ定義」を参照。

## 変更運用（eng-practices）

API 仕様変更に固有の運用:

- **破壊的変更の最小化**: 必須フィールド追加、型変更、パス削除、認証スコープ変更などは原則 2 段階（追加 → 旧削除）に分け、`deprecated` と移行期間を仕様と CL 説明に明示する。
- **影響範囲を CL に明示**: 公開 API の Breaking change／クライアント影響／バージョニング扱い／移行手順／ロールバック計画を PR 本文に書く。

Small CL の分け方、Why の残し方などの共通原則は `eng-practices` スキルを参照。

## レビューワークフロー

### チェック項目

1. **リソース設計**
   - 適切な名詞が使用されているか
   - 階層関係が正しく表現されているか
   - 冗長な endpoint がないか

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

## リファレンス

詳細なガイドは以下を参照:

- **OpenAPI仕様詳細・CRUD実例**: [references/openapi-spec.md](references/openapi-spec.md)
- **ベストプラクティス（エラーハンドリング定義一式を含む）**: [references/best-practices.md](references/best-practices.md)
