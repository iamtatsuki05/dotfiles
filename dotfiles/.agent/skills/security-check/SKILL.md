---
name: security-check
description: コードのセキュリティ脆弱性を検出・分析するための汎用スキル。OWASP Top 10に基づく脆弱性チェック、シークレット漏洩検出、インジェクション脆弱性、安全でないデシリアライゼーション、アクセス制御の不備などを検出。セキュリティレビュー、脆弱性診断、コード監査、セキュリティチェック、ペネトレーションテスト準備、セキュアコーディングレビュー時に使用。
---

# Security Check

コードベースのセキュリティ脆弱性を体系的に検出・分析する。

## ワークフロー

```
セキュリティチェック依頼
    │
    ├─ 特定ファイル/PR → 対象コードを読み取り、直接分析
    │
    └─ プロジェクト全体 → 以下のフェーズを順に実行
         │
         ├─ Phase 1: シークレット・機密情報スキャン
         ├─ Phase 2: インジェクション脆弱性スキャン
         ├─ Phase 3: 認証・認可チェック
         ├─ Phase 4: 依存関係チェック
         └─ Phase 5: レポート生成
```

## Phase 1: シークレット・機密情報スキャン

Grepで以下のパターンを検索:

```bash
# APIキー・シークレット
rg -i "(api[_-]?key|secret|password|token|credential)\s*[:=]\s*['\"][^'\"]+['\"]"

# AWSキー
rg "AKIA[0-9A-Z]{16}"

# プライベートキー
rg "-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"

# 環境変数の直接埋め込み
rg -i "(DB_PASSWORD|JWT_SECRET|STRIPE_KEY)\s*="
```

**除外対象**: `.env.example`, `*.test.*`, `*_test.go`, `mock*`

## Phase 2: インジェクション脆弱性スキャン

### SQLインジェクション
```bash
# 文字列連結によるSQL構築
rg "SELECT.*FROM.*WHERE.*\+|f['\"]SELECT|format.*SELECT"
rg "execute\(.*\+|query\(.*\+"
```

### コマンドインジェクション
```bash
# シェルコマンド実行
rg "os\.system\(|subprocess.*shell=True|exec\(|eval\("
rg "child_process\.exec\(|spawn.*shell:"
rg "Runtime\.getRuntime\(\)\.exec\("
```

### XSS
```bash
# 安全でないHTML出力
rg "innerHTML\s*=|dangerouslySetInnerHTML|v-html="
rg "\.html\(.*\$|document\.write\("
```

### デシリアライゼーション
```bash
rg "pickle\.load|yaml\.load\((?!.*SafeLoader)|unserialize\(|ObjectInputStream"
```

## Phase 3: 認証・認可チェック

### 認証の確認事項
- パスワードハッシュアルゴリズム（bcrypt/Argon2推奨）
- セッション管理の安全性
- JWTの署名検証と有効期限

### 認可の確認事項
```bash
# 認可チェックの欠如を探す
rg "@app\.(get|post|put|delete).*def \w+\(" --type py
rg "router\.(get|post|put|delete)" --type ts
```

各エンドポイントで適切な認可チェックが行われているか確認。

## Phase 4: 依存関係チェック

対象ファイルを確認:
- `package.json` / `package-lock.json`
- `requirements.txt` / `Pipfile.lock`
- `go.mod` / `go.sum`
- `Gemfile.lock`
- `pom.xml` / `build.gradle`

既知の脆弱性がないかバージョンを確認。

## Phase 5: レポート生成

### レポートフォーマット

```markdown
# セキュリティチェックレポート

**対象**: [プロジェクト名/ファイル名]
**日時**: [YYYY-MM-DD]

## サマリー
- 🔴 Critical: X件
- 🟠 High: X件
- 🟡 Medium: X件
- 🔵 Low: X件

## 検出された脆弱性

### [Critical] タイトル
- **ファイル**: `path/to/file.py:123`
- **種類**: SQLインジェクション
- **説明**: ユーザー入力が直接SQLクエリに連結されている
- **影響**: データベースの不正アクセス、データ漏洩
- **修正案**:
  ```python
  # Before
  query = f"SELECT * FROM users WHERE id = {user_id}"

  # After
  cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
  ```

## 推奨事項
1. [優先度高] ...
2. [優先度中] ...
```

## 深刻度の判定基準

| 深刻度 | 基準 |
|--------|------|
| Critical | リモートコード実行、認証バイパス、機密データ漏洩 |
| High | SQLインジェクション、XSS、SSRF |
| Medium | 弱い暗号化、セッション管理の不備 |
| Low | 情報漏洩（バージョン情報等）、ベストプラクティス違反 |

## リファレンス

詳細なチェックリストが必要な場合:

- **OWASP Top 10詳細**: [references/owasp-checklist.md](references/owasp-checklist.md)
- **言語別パターン**: [references/language-specific.md](references/language-specific.md)
