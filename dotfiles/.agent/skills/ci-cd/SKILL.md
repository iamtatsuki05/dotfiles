---
name: ci-cd
description: CI/CDパイプラインの作成、編集、デバッグ、最適化を支援する汎用スキル。GitHub Actions、GitLab CI、CircleCI、Jenkins等のワークフロー設計・実装。YAMLファイル(.yml/.yaml)の作成・編集、パイプライン構成、ジョブ・ステップ定義、シークレット管理、キャッシュ設定、マトリックスビルド、デプロイメント自動化時に使用。「CIを設定」「パイプライン作成」「GitHub Actions追加」「デプロイ自動化」等のリクエストでトリガー。
---

# CI/CDスキル

CI/CDパイプラインの設計、実装、デバッグ、最適化を効率的に行うためのガイド。

## 実装前の必須確認

1. **既存のCI/CD設定を確認**: `.github/workflows/`, `.gitlab-ci.yml`, `.circleci/`, `Jenkinsfile`
2. **プロジェクト構成を確認**: 言語、フレームワーク、ビルドツール、テストフレームワーク
3. **デプロイ先を確認**: クラウドプロバイダー、コンテナレジストリ、Kubernetes等

## プラットフォーム別クイックスタート

### GitHub Actions

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: 'npm'

      - name: Install dependencies
        run: npm ci

      - name: Run tests
        run: npm test

      - name: Build
        run: npm run build
```

### GitLab CI

```yaml
# .gitlab-ci.yml
stages:
  - test
  - build
  - deploy

variables:
  NODE_VERSION: "20"

test:
  stage: test
  image: node:${NODE_VERSION}
  cache:
    key: ${CI_COMMIT_REF_SLUG}
    paths:
      - node_modules/
  script:
    - npm ci
    - npm test

build:
  stage: build
  image: node:${NODE_VERSION}
  script:
    - npm ci
    - npm run build
  artifacts:
    paths:
      - dist/
```

### CircleCI

```yaml
# .circleci/config.yml
version: 2.1

orbs:
  node: circleci/node@5

jobs:
  build-and-test:
    docker:
      - image: cimg/node:20.0
    steps:
      - checkout
      - node/install-packages:
          pkg-manager: npm
      - run:
          name: Run tests
          command: npm test
      - run:
          name: Build
          command: npm run build

workflows:
  main:
    jobs:
      - build-and-test
```

## ベストプラクティス

### キャッシュ戦略

```yaml
# GitHub Actions
- uses: actions/cache@v4
  with:
    path: |
      ~/.npm
      node_modules
    key: ${{ runner.os }}-node-${{ hashFiles('**/package-lock.json') }}
    restore-keys: |
      ${{ runner.os }}-node-

# Go modules
- uses: actions/cache@v4
  with:
    path: ~/go/pkg/mod
    key: ${{ runner.os }}-go-${{ hashFiles('**/go.sum') }}

# Python pip
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('**/requirements.txt') }}
```

### マトリックスビルド

```yaml
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        node-version: [18, 20, 22]
        exclude:
          - os: macos-latest
            node-version: 18
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node-version }}
```

### シークレット管理

```yaml
# 環境変数でシークレット参照
env:
  DATABASE_URL: ${{ secrets.DATABASE_URL }}
  API_KEY: ${{ secrets.API_KEY }}

# 環境ごとのシークレット
jobs:
  deploy:
    environment: production
    env:
      AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
```

### 条件付き実行

```yaml
# パス条件
on:
  push:
    paths:
      - 'src/**'
      - 'package.json'
    paths-ignore:
      - '**.md'
      - 'docs/**'

# ジョブ条件
jobs:
  deploy:
    if: github.ref == 'refs/heads/main' && github.event_name == 'push'
```

## デプロイパターン

### Docker Build & Push

```yaml
jobs:
  build-push:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Login to Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ghcr.io/${{ github.repository }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

### Kubernetes デプロイ

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup kubectl
        uses: azure/setup-kubectl@v3

      - name: Configure kubeconfig
        run: |
          echo "${{ secrets.KUBECONFIG }}" | base64 -d > kubeconfig
          export KUBECONFIG=kubeconfig

      - name: Deploy
        run: |
          kubectl set image deployment/app \
            app=ghcr.io/${{ github.repository }}:${{ github.sha }}
          kubectl rollout status deployment/app
```

### AWS デプロイ

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/deploy-role
          aws-region: ap-northeast-1

      - name: Deploy to ECS
        run: |
          aws ecs update-service \
            --cluster production \
            --service app \
            --force-new-deployment
```

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

- name: Run Semgrep
  uses: returntocorp/semgrep-action@v1
```

## リファレンス

詳細なガイドは以下を参照:

- **GitHub Actions詳細**: [references/github-actions.md](references/github-actions.md)
- **GitLab CI詳細**: [references/gitlab-ci.md](references/gitlab-ci.md)
- **デプロイパターン集**: [references/deploy-patterns.md](references/deploy-patterns.md)
