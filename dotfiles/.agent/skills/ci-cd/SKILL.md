---
name: ci-cd
description: "Use when the user asks to create, edit, debug, or optimize CI/CD pipelines: workflow YAML, build/test jobs, matrix builds, caches, permissions, secrets, or deployment automation in GitHub Actions, GitLab CI, CircleCI, or Jenkins. Not for application code that merely fails in CI (use auto-debugger) or for branch/PR operations (use git-github-flow)."
---

# CI/CD

CI/CD workflow の作成・修正・デバッグを、本番影響と権限を確認したうえで最小差分で行い、実際の CI 実行まで確認して報告する。

## 着手前に確認すること

1. 既存設定: `.github/workflows/`、`.gitlab-ci.yml`、`.circleci/config.yml`、`Jenkinsfile` と、周囲の action ピン方針(タグ / commit SHA)。
2. プロジェクト構成: 言語、ビルドツール、テストコマンド、lockfile。
3. trigger と本番影響: push / pull_request / schedule / workflow_dispatch のどれで動くか、deploy job が本番環境に触れるか。
4. 権限と secret: `permissions` の現状と、参照している secret 名(値は出力しない)。
5. 変更対象外の job、branch 条件、cache key、artifact retention は触らない。

## 書き方の規則

- **action のバージョン**: 既存 repo ではピン方針に合わせる。新規なら各 action の最新 major を確認して選び、`@master` / `@main` は使わない。
- **permissions**: workflow または job 単位で最小権限を明示する(例: `contents: read`)。cloud 認証は長期キーでなく OIDC(`id-token: write`)を優先する。
- **secret**: 環境ごとに分け、deploy job は `environment:` に紐付けて保護ルール(承認、branch 制限)を効かせる。値をログに出さない。
- **cache**: `actions/setup-node` などの built-in cache(`cache: 'npm'`)を優先する。`actions/cache` を直接使うなら key は lockfile の `hashFiles()`、`restore-keys` で prefix fallback を用意する。cache key を変えるときは他 workflow への影響を確認する。
- **matrix**: サポート対象バージョンだけを列挙し、EOL バージョンを入れない。組み合わせは `exclude` / `include` で絞る。
- **条件付き実行**: `paths` / `paths-ignore` で不要な実行を減らす。deploy job は branch と event の両方で限定する(例: `github.ref == 'refs/heads/main' && github.event_name == 'push'`)。
- **concurrency**: 同一 ref の重複実行は `concurrency` + `cancel-in-progress: true` で止める。deploy job には `cancel-in-progress` を付けない。
- **YAML コメント**: `if:` 条件、`continue-on-error: true`、独自 retry など読みにくい分岐には理由を 1 行残す。

## プラットフォーム別

GitHub Actions を基準に差分だけ押さえる。完全な YAML を書く段階でだけ reference を読む。

- **GitHub Actions**: lint → test → build → deploy の完全例、reusable workflow、composite action、service container は [references/github-actions.md](references/github-actions.md)。
- **GitLab CI**(`.gitlab-ci.yml`): job を `stages` で順序付け、step の代わりに `image` + `script` を書く。再利用は `include` / `extends`、条件は `rules`。詳細は [references/gitlab-ci.md](references/gitlab-ci.md)。
- **CircleCI**(`.circleci/config.yml`): 再利用単位は orbs。`jobs` を `workflows` で組み合わせ、依存は `requires` で宣言する。最小例は [references/circleci.md](references/circleci.md)。
- **デプロイ**(Docker build & push、Kubernetes、AWS ECS / Lambda、Cloud Run、静的サイト): 承認フロー、environment protection rules、ロールバック手段を先に確認してから [references/deploy-patterns.md](references/deploy-patterns.md) の例を使う。

## セキュリティスキャンを足すとき

- 依存・コンテナ: `aquasecurity/trivy-action`(release tag にピン、`severity: 'CRITICAL,HIGH'`)。
- SAST: `github/codeql-action/init` → `github/codeql-action/analyze`(job に `security-events: write` が必要)。
- Semgrep: `returntocorp/semgrep-action` は廃止済み。`container: { image: semgrep/semgrep }` の job で `semgrep ci`(Semgrep AppSec Platform 連携なしなら `semgrep scan --config auto`)を実行する。

## 失敗の調査

- ログは `gh run view <run-id> --log-failed` で失敗 step だけ取る。step debug が必要なら `gh run rerun <run-id> --debug` を使う。workflow の `env:` に `ACTIONS_STEP_DEBUG` を書いても有効にならない(repository の secret / variable か再実行時の指定が必要)。
- 失敗が app コードやテスト自体の問題なら、workflow ではなくコードを直す(`auto-debugger`)。workflow 側の原因は、runner 環境差(OS、ツールバージョン)、stale な cache、権限不足、secret 未設定、`if:` 条件の順に疑う。

## 検証と報告

- プラットフォームの lint を実行する: GitHub Actions は `actionlint`、GitLab CI は Pipeline Editor の CI Lint か `POST /projects/:id/ci/lint` API、CircleCI は `circleci config validate`。未導入なら `missing-tools` skill で一時実行する。
- 可能ならローカルで同等の build / test コマンドを実行する。CI 上でしか確認できない場合はその前提を報告する。
- push 済みなら `gh run watch` / `gh run view` で実際の CI 実行が成功するまで確認してから完了報告する。ローカル成功だけで「直った」と報告しない。
- デプロイ、`permissions` の昇格、secret 追加、外部サービス連携を伴う変更は、対象環境と影響を明示してユーザー承認を取ってから進める。
- PR description には trigger 変更、`permissions` の変更、secret 追加、cache key 変更、deploy 影響を明示する。`eng-practices` は PR description を書く段階でだけ読む。
- 最終報告には、変更した workflow / job、trigger、権限、secret 参照、実行した検証(lint、ローカル実行、CI run の URL または ID)、残るリスクを含める。
