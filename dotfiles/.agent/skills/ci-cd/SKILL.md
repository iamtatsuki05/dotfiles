---
name: ci-cd
description: "Use when the user asks to create, edit, debug, or optimize CI/CD pipelines, workflow YAML, build/test jobs, deployment automation, matrix builds, caches, permissions, or secrets in GitHub Actions, GitLab CI, CircleCI, Jenkins, or similar systems."
---

# CI/CDスキル

CI/CDパイプラインの設計、実装、デバッグ、最適化を効率的に行うためのガイド。

## 実装前の必須確認

1. **既存のCI/CD設定を確認**: `.github/workflows/`, `.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`
2. **プロジェクト構成を確認**: 言語、フレームワーク、ビルドツール、テストフレームワーク
3. **デプロイ先を確認**: クラウドプロバイダー、コンテナレジストリ、Kubernetes等
4. **本番影響を確認**: push / pull_request / schedule / workflow_dispatch のどれで動くか、deploy job が本番に触れるか
5. **権限とsecretを確認**: `permissions` は最小権限にし、secret 名は参照だけに留め、値を出力しない
6. **既存workflowの意図を確認**: 無関係な job、branch 条件、cache key、artifact retention を変更しない

## プラットフォーム別クイックスタート

### GitHub Actions

完全なワークフロー例（lint → test → build → deploy）は [references/github-actions.md](references/github-actions.md) の「ワークフロー構文」を参照。書くときの注意:

- action のバージョン（`actions/checkout@v4` など）は執筆時点のもの。major は各 action の最新安定版を確認して選び、既存 repo では周囲のピン方針（タグ / commit SHA）に合わせる。

### 他プラットフォームとの差分

GitHub Actions の骨子（trigger → job → step）を基準に、主な対応関係だけ押さえる。

- **GitLab CI**（`.gitlab-ci.yml`）: job を `stages` で順序付けし、step の代わりに `image` + `script` を書く。action の代わりに `include` / `extends` でテンプレートを再利用し、cache / artifacts はキーワードで宣言する。完全な YAML 例と rules・親子パイプライン等は [references/gitlab-ci.md](references/gitlab-ci.md) 参照。
- **CircleCI**（`.circleci/config.yml`）: 再利用単位は orbs。`jobs` を `workflows` で組み合わせ、依存は `requires` で宣言する。実行環境は `docker` / `machine` / `macos` の executor で指定する。最小構成例は [references/circleci.md](references/circleci.md) 参照。

## ベストプラクティス

- **キャッシュ**: `actions/setup-node` 等の built-in cache（`cache: 'npm'`）を優先する。`actions/cache` を直接使う場合は lockfile の `hashFiles()` を key にし、`restore-keys` で prefix fallback を用意する。cache key の変更は既存 workflow への影響を確認する。
- **マトリックス**: サポート対象バージョンだけを列挙し、EOL バージョンを matrix に入れない（最新の LTS 状況を確認）。組み合わせ爆発は `exclude` / `include` で制御する。
- **permissions**: workflow / job 単位で最小権限を明示し（例: `contents: read`）、デフォルトの広い権限に依存しない。クラウド認証は長期キーでなく OIDC（`id-token: write`）を優先する。
- **シークレット**: 環境ごとに分離し、deploy job は `environment:` に紐付けて保護ルール（承認、branch 制限）を効かせる。値をログに出力しない。
- **条件付き実行**: `paths` / `paths-ignore` で不要な実行を減らし、deploy job は branch + event の条件（例: `github.ref == 'refs/heads/main' && github.event_name == 'push'`）で限定する。

YAML の具体例は [references/github-actions.md](references/github-actions.md) を参照。

## デプロイパターン

デプロイ系の変更は、承認フロー・環境保護ルール（environment protection rules）・ロールバック手段を先に確認する。Docker build & push、Kubernetes（kubectl / Helm / Kustomize / ArgoCD）、AWS ECS / Lambda などのパターン例は [references/deploy-patterns.md](references/deploy-patterns.md) を参照。

## デバッグ

### ログ出力

```yaml
# デバッグモード有効化
env:
  ACTIONS_STEP_DEBUG: true

# 手動デバッグ出力
- name: Debug info
  run: |
    echo "Event: ${{ github.event_name }}"
    echo "Ref: ${{ github.ref }}"
    echo "SHA: ${{ github.sha }}"
```

### 失敗時の対応

```yaml
- name: Upload logs on failure
  if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: logs
    path: |
      *.log
      test-results/
```

## セキュリティ

### 依存関係スキャン

```yaml
jobs:
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          scan-type: 'fs'
          scan-ref: '.'
          severity: 'CRITICAL,HIGH'
```

### SAST（静的解析）

```yaml
- name: Run CodeQL
  uses: github/codeql-action/analyze@v3
```

Semgrep は `returntocorp/semgrep-action` が廃止済みのため、公式コンテナで `semgrep ci` を実行する。

```yaml
jobs:
  semgrep:
    runs-on: ubuntu-latest
    container:
      image: semgrep/semgrep
    steps:
      - uses: actions/checkout@v4
      # Semgrep AppSec Platform 連携なしなら `semgrep scan --config auto`
      - run: semgrep ci
        env:
          SEMGREP_APP_TOKEN: ${{ secrets.SEMGREP_APP_TOKEN }}
```

## PR 運用（eng-practices）

Small CL、PR 説明の書き方などの共通原則は `eng-practices` スキル参照。CI/CD 固有には以下を徹底する。

- **意図と影響を PR に書く**: trigger 変更、`permissions` の昇格、secret 追加、外部サービス連携、deploy 影響、cache key 変更を本文で明示する。
- **Why を YAML コメントに残す**: `if:` 条件、`continue-on-error: true`、独自 retry など読みにくい分岐には理由コメントを 1 行残す。

## 実装後の検証と報告

- YAML 構文検証、既存の lint に加え、プラットフォームに合う検証を実行する: GitHub Actions は `actionlint`、GitLab CI は CI Lint（Web UI の Pipeline Editor、または `POST /projects/:id/ci/lint` API）、CircleCI は `circleci config validate`。`actionlint` や `circleci` CLI が未導入の場合は `missing-tools` skill で一時実行する。
- 可能ならローカルで同等の build/test コマンドを実行する。CI 上でしか確認できない場合は、その前提を報告する。
- デプロイ、権限拡大、secret 追加、外部サービス更新を伴う変更は、対象環境と影響を明示してユーザー承認を取る。
- 最終報告には、変更した workflow/job、trigger、権限、secret 参照、実行した検証、残るリスクを含める。

## リファレンス

詳細なガイドは以下を参照:

- **GitHub Actions詳細**: [references/github-actions.md](references/github-actions.md)
- **GitLab CI詳細**: [references/gitlab-ci.md](references/gitlab-ci.md)
- **CircleCI最小構成**: [references/circleci.md](references/circleci.md)
- **デプロイパターン集**: [references/deploy-patterns.md](references/deploy-patterns.md)
