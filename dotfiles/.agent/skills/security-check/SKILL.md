---
name: security-check
description: "Use when the user explicitly asks for a security review, vulnerability scan, secret-leak detection, OWASP-style audit, dependency audit, or injection/auth/access-control review. Not for 'safety' reviews of destructive operations, job launchers, rollouts, or rollback logic, nor ordinary code review; do those as normal reviews."
---

# Security Check

攻撃者視点でコードベースの脆弱性(secret 露出、injection、認証・認可の欠落、脆弱な依存)を検出し、深刻度付きで報告する。

## 対象外

「safety review」「production-safety」「destructive-operation safety」と書かれた依頼は、この skill の対象ではない。Slurm launcher や削除 script の fail-closed 境界、TOCTOU、rollback、data-loss の検証は依頼文の形式に従う通常のレビューで行い、alias / fallback / legacy path の判断は `compatibility-safety` を使う。この skill を読むのは、依頼が攻撃面(secret、injection、auth、依存)を明示したときだけにする。

## 進め方

- 特定ファイル / PR が対象: 対象コードを読み、Phase 1 の grep を対象範囲に実行し、Phase 2〜3 の観点で直接分析する。Phase 4 は依存ファイルが差分に含まれるときだけ行う。
- project 全体または未知範囲が対象: Phase 1〜5 を順に実行する。
- 文書や設定ファイルの漏洩チェックだけなら Phase 1 だけで終える。

## Phase 1: secret スキャン(単独利用可)

```bash
rg -n -i "(api[_-]?key|secret|password|token|credential)\s*[:=]\s*['\"][^'\"]+['\"]" \
  -g '!*.example' -g '!*.test.*' -g '!*_test.go' -g '!mock*' -g '!*.lock'
rg -n "AKIA[0-9A-Z]{16}"
rg -n "-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
rg -n -i "(DB_PASSWORD|JWT_SECRET|STRIPE_KEY|GITHUB_TOKEN|OPENAI_API_KEY)\s*="
rg -n "(ghp|gho|ghu|ghs)_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]+"
```

検出した候補は値を再掲しない(先頭 / 末尾数文字もマスクする)。ファイル、行、種類、露出の疑い(実害あり / 要確認 / 誤検知)を示す。`.env.example` やテスト fixture でも実在形式のキーに見えるものは要確認に入れる。

## Phase 2: injection スキャン

```bash
# SQL: 文字列連結・f-string・format による組み立て
rg -n "SELECT.*FROM.*WHERE.*\+|f['\"]SELECT|format.*SELECT|execute\(.*\+|query\(.*\+"
# コマンド実行
rg -n "os\.system\(|subprocess.*shell=True|exec\(|eval\(|child_process\.exec\(|spawn.*shell:|Runtime\.getRuntime\(\)\.exec\("
# XSS
rg -n "innerHTML\s*=|dangerouslySetInnerHTML|v-html=|\.html\(.*\$|document\.write\("
# デシリアライゼーション(yaml.load の look-ahead には -P が必要。ヒット後に SafeLoader 指定を目視確認)
rg -n -P "pickle\.load|yaml\.load\((?!.*SafeLoader)|unserialize\(|ObjectInputStream"
```

ヒット行は、入力元がユーザー制御かどうかと、サニタイズ / パラメータ化の有無を目視で確認してから findings にする。対象言語が Python / JavaScript / TypeScript 以外(Go、Java、Ruby、PHP)なら [references/language-specific.md](references/language-specific.md) の pattern を足す。

## Phase 3: 認証・認可

- 認証: パスワードハッシュ(bcrypt / Argon2 以外は指摘)、セッション管理、JWT の署名検証と有効期限。
- 認可: ルート定義を列挙し、各エンドポイントで認可チェックが行われているか確認する。

```bash
rg -n "@app\.(get|post|put|delete).*def \w+\(" --type py
rg -n "router\.(get|post|put|delete)" --type ts
```

## Phase 4: 依存関係

`package-lock.json` / `requirements.txt` / `Pipfile.lock` / `go.sum` / `Gemfile.lock` / `pom.xml` / `build.gradle` を対象に、利用可能なツールで監査する。

```bash
npm audit --audit-level=high
pip-audit            # uv 環境なら uv run pip-audit
govulncheck ./...
bundle audit
```

未導入のツールは勝手に追加せず、実行できなかったことを報告するか、必要性を説明して確認する。

## Phase 5: レポート

OWASP Top 10 での網羅を求められた場合は、書く前に [references/owasp-checklist.md](references/owasp-checklist.md) の A01〜A10 を順に確認する。

```markdown
# セキュリティチェックレポート
**対象**: [project / file]  **日時**: [YYYY-MM-DD]

## サマリー
Critical: X件 / High: X件 / Medium: X件 / Low: X件

## 検出された脆弱性
### [Critical] タイトル
- **ファイル**: `path/to/file.py:123`
- **種類**: SQL injection
- **説明**: ユーザー入力が直接 SQL に連結されている
- **影響**: DB の不正アクセス、データ漏洩
- **修正案**: `cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))`

## 推奨事項
1. [優先度高] ...
```

| 深刻度 | 基準 |
|--------|------|
| Critical | リモートコード実行、認証バイパス、機密データ漏洩 |
| High | SQL injection、XSS、SSRF |
| Medium | 弱い暗号化、セッション管理の不備 |
| Low | 情報漏洩(バージョン情報など)、ベストプラクティス違反 |

## 報告ルール

- レビュー専用依頼では修正しない。修正まで求められた場合だけ最小変更で対応する。
- 誤検知、要確認、実害ありを分けて記載する。
- 出力形式は依頼文の指定を優先する(reviewer 依頼なら重大 / 中 / 軽微 + file:line、問題がなければ「重大な問題なし」)。指定がなければ Phase 5 の形式を使う。
- 最終報告には、対象範囲、実行した grep / ツール(未導入で実行できなかったものも)、検出件数、値をマスクした findings、未確認範囲を含める。
