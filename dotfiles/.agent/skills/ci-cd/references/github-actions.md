# GitHub Actions 詳細リファレンス

## 目次

1. [ワークフロー構文](#ワークフロー構文)
2. [トリガー詳細](#トリガー詳細)
3. [ジョブ設定](#ジョブ設定)
4. [Reusable Workflows](#reusable-workflows)
5. [Composite Actions](#composite-actions)
6. [よく使うActions](#よく使うactions)

## ワークフロー構文

### 完全な構文例

```yaml
name: Complete CI/CD Pipeline

on:
  push:
    branches: [main, develop]
    tags: ['v*']
  pull_request:
    branches: [main]
  schedule:
    - cron: '0 0 * * 0'  # 毎週日曜0時
  workflow_dispatch:
    inputs:
      environment:
        description: 'Deploy environment'
        required: true
        default: 'staging'
        type: choice
        options:
          - staging
          - production

env:
  NODE_VERSION: '20'
  REGISTRY: ghcr.io

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
  packages: write
  id-token: write

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: 'npm'
      - run: npm ci
      - run: npm run lint

  test:
    needs: lint
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        shard: [1, 2, 3]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ env.NODE_VERSION }}
          cache: 'npm'
      - run: npm ci
      - run: npm test -- --shard=${{ matrix.shard }}/3

  build:
    needs: test
    runs-on: ubuntu-latest
    outputs:
      image-tag: ${{ steps.meta.outputs.tags }}
    steps:
      - uses: actions/checkout@v4

      - name: Docker meta
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ github.repository }}
          tags: |
            type=ref,event=branch
            type=ref,event=pr
            type=semver,pattern={{version}}
            type=sha

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: build
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: production
    steps:
      - name: Deploy
        run: echo "Deploying ${{ needs.build.outputs.image-tag }}"
```

## トリガー詳細

### push/pull_request フィルター

```yaml
on:
  push:
    branches:
      - main
      - 'release/**'
      - '!release/**-beta'  # 除外
    tags:
      - 'v*'
    paths:
      - 'src/**'
      - 'package.json'
    paths-ignore:
      - '**.md'
      - '.github/**'
```

### workflow_dispatch（手動実行）

```yaml
on:
  workflow_dispatch:
    inputs:
      version:
        description: 'Release version'
        required: true
        type: string
      dry-run:
        description: 'Dry run mode'
        required: false
        type: boolean
        default: false
      log-level:
        description: 'Log level'
        required: true
        type: choice
        options:
          - debug
          - info
          - warning
          - error
```

### repository_dispatch（外部トリガー）

```yaml
on:
  repository_dispatch:
    types: [deploy-production, rollback]

jobs:
  handle:
    runs-on: ubuntu-latest
    steps:
      - run: echo "Event type: ${{ github.event.action }}"
      - run: echo "Payload: ${{ github.event.client_payload.ref }}"
```

## ジョブ設定

### サービスコンテナ

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:15
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

      redis:
        image: redis:7
        ports:
          - 6379:6379

    steps:
      - uses: actions/checkout@v4
      - run: npm test
        env:
          DATABASE_URL: postgres://postgres:postgres@localhost:5432/test
          REDIS_URL: redis://localhost:6379
```

### 依存関係とOutputs

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.version.outputs.value }}
    steps:
      - id: version
        run: echo "value=$(cat VERSION)" >> $GITHUB_OUTPUT

  deploy:
    needs: [build, test]
    runs-on: ubuntu-latest
    steps:
      - run: echo "Deploying version ${{ needs.build.outputs.version }}"
```

### 動的マトリックス

```yaml
jobs:
  setup:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.set-matrix.outputs.matrix }}
    steps:
      - id: set-matrix
        run: |
          echo "matrix={\"include\":[{\"project\":\"api\"},{\"project\":\"web\"}]}" >> $GITHUB_OUTPUT

  build:
    needs: setup
    runs-on: ubuntu-latest
    strategy:
      matrix: ${{ fromJson(needs.setup.outputs.matrix) }}
    steps:
      - run: echo "Building ${{ matrix.project }}"
```

## Reusable Workflows

### 定義側（.github/workflows/reusable-build.yml）

```yaml
name: Reusable Build

on:
  workflow_call:
    inputs:
      node-version:
        required: false
        type: string
        default: '20'
      environment:
        required: true
        type: string
    secrets:
      npm-token:
        required: true
    outputs:
      artifact-name:
        description: 'Built artifact name'
        value: ${{ jobs.build.outputs.artifact }}

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      artifact: ${{ steps.build.outputs.name }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ inputs.node-version }}
      - run: npm ci
        env:
          NPM_TOKEN: ${{ secrets.npm-token }}
      - id: build
        run: |
          npm run build
          echo "name=dist-${{ github.sha }}" >> $GITHUB_OUTPUT
```

### 呼び出し側

```yaml
jobs:
  call-build:
    uses: ./.github/workflows/reusable-build.yml
    with:
      node-version: '20'
      environment: production
    secrets:
      npm-token: ${{ secrets.NPM_TOKEN }}

  deploy:
    needs: call-build
    runs-on: ubuntu-latest
    steps:
      - run: echo "Artifact: ${{ needs.call-build.outputs.artifact-name }}"
```

## Composite Actions

### action.yml（.github/actions/setup-project/action.yml）

```yaml
name: 'Setup Project'
description: 'Setup Node.js and install dependencies'

inputs:
  node-version:
    description: 'Node.js version'
    required: false
    default: '20'
  install-command:
    description: 'Install command'
    required: false
    default: 'npm ci'

outputs:
  cache-hit:
    description: 'Cache hit'
    value: ${{ steps.cache.outputs.cache-hit }}

runs:
  using: 'composite'
  steps:
    - name: Setup Node.js
      uses: actions/setup-node@v4
      with:
        node-version: ${{ inputs.node-version }}

    - name: Cache dependencies
      id: cache
      uses: actions/cache@v4
      with:
        path: node_modules
        key: ${{ runner.os }}-node-${{ hashFiles('**/package-lock.json') }}

    - name: Install dependencies
      if: steps.cache.outputs.cache-hit != 'true'
      shell: bash
      run: ${{ inputs.install-command }}
```

### 使用例

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ./.github/actions/setup-project
        with:
          node-version: '20'
      - run: npm run build
```

## よく使うActions

### セットアップ系

| Action | 用途 |
|--------|------|
| `actions/setup-node@v4` | Node.js |
| `actions/setup-python@v5` | Python |
| `actions/setup-go@v5` | Go |
| `actions/setup-java@v4` | Java |
| `ruby/setup-ruby@v1` | Ruby |
| `dtolnay/rust-toolchain@stable` | Rust |

### ビルド・デプロイ系

| Action | 用途 |
|--------|------|
| `docker/build-push-action@v5` | Docker build & push |
| `docker/login-action@v3` | Container registry login |
| `aws-actions/configure-aws-credentials@v4` | AWS認証 |
| `google-github-actions/auth@v2` | GCP認証 |
| `azure/login@v1` | Azure認証 |

### ユーティリティ系

| Action | 用途 |
|--------|------|
| `actions/cache@v4` | キャッシュ |
| `actions/upload-artifact@v4` | アーティファクトアップロード |
| `actions/download-artifact@v4` | アーティファクトダウンロード |
| `peter-evans/create-pull-request@v6` | PR自動作成 |
| `softprops/action-gh-release@v2` | GitHub Release作成 |
