# OWASP Top 10 チェックリスト

2021 版ベース。最新版のカテゴリ変更にも注意する。静的検索で検出しやすいカテゴリの確認観点と rg パターンのみを載せる。依存関係（A06 相当）は SKILL.md Phase 4、認証・セッション（A07 相当）は SKILL.md Phase 3 を参照。A04（安全でない設計）と A09（ログとモニタリングの不備）は静的検索では検出しにくいため本チェックリストの対象外とし、設計レビューや運用確認で扱う。

## A01 - アクセス制御の不備

- 認可チェックなしの API エンドポイント、IDOR、権限昇格、CORS の誤設定

```bash
# ルート定義を列挙し、各ヒット行で認可チェック（認証依存・権限判定）の有無を確認する
rg "@app\.(get|post|put|delete)" --type py
rg "router\.(get|post|put|delete)" --type ts
```

## A02 - 暗号化の失敗

- ハードコードされたシークレット、弱いハッシュ（MD5/SHA1）、HTTP 経由の機密データ送信、証明書検証の無効化

```bash
rg -i "(password|api_key|secret|token)\s*=\s*[\"']"
rg "hashlib\.(md5|sha1)"
rg -i "verify\s*=\s*False|InsecureSkipVerify"
```

## A03 - インジェクション

- SQL / OS コマンド / XSS / テンプレートインジェクション
- rg パターンは SKILL.md Phase 2 のものを使用する

## A05 - セキュリティ設定のミス

- デバッグモード有効、デフォルト認証情報、詳細すぎるエラーメッセージ

```bash
rg -i "DEBUG\s*=\s*True|debug=true|FLASK_DEBUG"
```

## A08 - ソフトウェアとデータの整合性の失敗

- 署名なしデータの受け入れ、安全でないデシリアライゼーション、CI/CD パイプラインの改ざん耐性

```bash
rg "pickle\.load|yaml\.load\(|eval\(|exec\("
```

## A10 - SSRF

- ユーザー入力の URL/IP をそのままサーバー側リクエストに使用、内部サービスへの到達、リダイレクトの悪用

```bash
rg "requests\.(get|post)\(.*request\.|fetch\(.*(req|request)\.(query|params|body)"
```
