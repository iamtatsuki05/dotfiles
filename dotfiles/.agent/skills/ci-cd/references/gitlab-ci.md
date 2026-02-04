# GitLab CI 詳細リファレンス

## 目次

1. [基本構文](#基本構文)
2. [パイプライン設計](#パイプライン設計)
3. [キャッシュとアーティファクト](#キャッシュとアーティファクト)
4. [環境とデプロイ](#環境とデプロイ)
5. [高度な機能](#高度な機能)

## 基本構文

### 完全な構文例

```yaml
# .gitlab-ci.yml
default:
  image: node:20-alpine
  before_script:
    - npm ci --cache .npm --prefer-offline
  cache:
    key: ${CI_COMMIT_REF_SLUG}
    paths:
      - .npm/
      - node_modules/

variables:
  DOCKER_TLS_CERTDIR: "/certs"
  FF_USE_FASTZIP: "true"
  ARTIFACT_COMPRESSION_LEVEL: "fast"

stages:
  - validate
  - test
  - build
  - deploy

workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
    - if: $CI_COMMIT_TAG

include:
  - local: '.gitlab/ci/test.yml'
  - project: 'group/shared-ci'
    ref: main
    file: '/templates/docker-build.yml'

lint:
  stage: validate
  script:
    - npm run lint
  rules:
    - changes:
        - "src/**/*"
        - "*.js"
        - "*.json"

test:
  stage: test
  parallel: 3
  script:
    - npm test -- --shard=$CI_NODE_INDEX/$CI_NODE_TOTAL
  coverage: '/All files[^|]*\|[^|]*\s+([\d\.]+)/'
  artifacts:
    reports:
      junit: junit.xml
      coverage_report:
        coverage_format: cobertura
        path: coverage/cobertura-coverage.xml

build:
  stage: build
  script:
    - npm run build
  artifacts:
    paths:
      - dist/
    expire_in: 1 week

deploy:
  stage: deploy
  script:
    - echo "Deploying..."
  environment:
    name: production
    url: https://example.com
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      when: manual
```

## パイプライン設計

### Rules vs only/except

```yaml
# 推奨: rules を使用
job:
  rules:
    # MRの場合
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      when: always
    # mainブランチへのpush
    - if: $CI_COMMIT_BRANCH == "main"
      when: always
    # タグ
    - if: $CI_COMMIT_TAG
      when: always
    # 手動実行
    - if: $CI_PIPELINE_SOURCE == "web"
      when: manual
    # それ以外は実行しない
    - when: never

# 特定のファイル変更時のみ
job:
  rules:
    - changes:
        - src/**/*
        - package.json
      when: always
    - when: never

# 変数による制御
job:
  rules:
    - if: $DEPLOY_TO_PRODUCTION == "true"
      when: manual
    - if: $CI_COMMIT_BRANCH == "main"
      when: on_success
```

### 親子パイプライン

```yaml
# 親パイプライン
trigger-child:
  stage: build
  trigger:
    include: child-pipeline.yml
    strategy: depend

# 動的パイプライン生成
generate-config:
  stage: prepare
  script:
    - generate-gitlab-ci.sh > generated-config.yml
  artifacts:
    paths:
      - generated-config.yml

trigger-dynamic:
  stage: build
  trigger:
    include:
      - artifact: generated-config.yml
        job: generate-config
    strategy: depend
```

### マルチプロジェクトパイプライン

```yaml
deploy-downstream:
  stage: deploy
  trigger:
    project: group/downstream-project
    branch: main
    strategy: depend
  variables:
    UPSTREAM_COMMIT: $CI_COMMIT_SHA
```

## キャッシュとアーティファクト

### キャッシュ戦略

```yaml
# グローバルキャッシュ
default:
  cache:
    key: global-cache
    paths:
      - node_modules/

# ジョブ固有のキャッシュ
build:
  cache:
    - key: npm-$CI_COMMIT_REF_SLUG
      paths:
        - node_modules/
      policy: pull-push
    - key: build-cache
      paths:
        - .cache/
      policy: push

# ブランチごとのキャッシュ（フォールバック付き）
test:
  cache:
    key:
      files:
        - package-lock.json
      prefix: ${CI_JOB_NAME}
    paths:
      - node_modules/
    when: on_success
```

### アーティファクト設定

```yaml
build:
  script:
    - npm run build
  artifacts:
    paths:
      - dist/
      - coverage/
    exclude:
      - dist/**/*.map
    expire_in: 1 week
    when: on_success
    reports:
      junit: junit.xml
      coverage_report:
        coverage_format: cobertura
        path: coverage/cobertura-coverage.xml
      dotenv: build.env

# 依存関係による取得
deploy:
  stage: deploy
  dependencies:
    - build
  needs:
    - job: build
      artifacts: true
```

## 環境とデプロイ

### 環境定義

```yaml
deploy_staging:
  stage: deploy
  script:
    - deploy.sh staging
  environment:
    name: staging
    url: https://staging.example.com
    on_stop: stop_staging
    auto_stop_in: 1 week

stop_staging:
  stage: deploy
  script:
    - destroy.sh staging
  environment:
    name: staging
    action: stop
  when: manual
  rules:
    - if: $CI_COMMIT_BRANCH
      when: manual
```

### 動的環境

```yaml
deploy_review:
  stage: deploy
  script:
    - deploy.sh review-$CI_COMMIT_REF_SLUG
  environment:
    name: review/$CI_COMMIT_REF_SLUG
    url: https://$CI_COMMIT_REF_SLUG.review.example.com
    on_stop: stop_review
    auto_stop_in: 1 day
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"

stop_review:
  stage: deploy
  script:
    - destroy.sh review-$CI_COMMIT_REF_SLUG
  environment:
    name: review/$CI_COMMIT_REF_SLUG
    action: stop
  when: manual
```

### Protected環境

```yaml
deploy_production:
  stage: deploy
  script:
    - deploy.sh production
  environment:
    name: production
    url: https://example.com
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      when: manual
  resource_group: production
```

## 高度な機能

### サービスコンテナ

```yaml
test:
  image: node:20
  services:
    - name: postgres:15
      alias: db
      variables:
        POSTGRES_DB: test
        POSTGRES_USER: test
        POSTGRES_PASSWORD: test
    - name: redis:7
      alias: cache
  variables:
    DATABASE_URL: postgres://test:test@db:5432/test
    REDIS_URL: redis://cache:6379
  script:
    - npm test
```

### 並列実行とMatrix

```yaml
test:
  parallel: 5
  script:
    - npm test -- --shard=$CI_NODE_INDEX/$CI_NODE_TOTAL

test_matrix:
  parallel:
    matrix:
      - NODE_VERSION: ["18", "20", "22"]
        OS: ["alpine", "bullseye"]
  image: node:${NODE_VERSION}-${OS}
  script:
    - npm test
```

### extends と include

```yaml
# テンプレート定義
.node_template:
  image: node:20
  before_script:
    - npm ci
  cache:
    key: ${CI_COMMIT_REF_SLUG}
    paths:
      - node_modules/

# 継承
lint:
  extends: .node_template
  script:
    - npm run lint

test:
  extends: .node_template
  script:
    - npm test

# 外部ファイルからのinclude
include:
  # ローカルファイル
  - local: '.gitlab/ci/test.yml'
  # 他プロジェクト
  - project: 'group/shared-templates'
    ref: main
    file: '/templates/docker.yml'
  # リモートURL
  - remote: 'https://example.com/templates/security.yml'
  # テンプレート
  - template: Security/SAST.gitlab-ci.yml
```

### 変数とシークレット

```yaml
variables:
  # 通常の変数
  NODE_ENV: production

  # 展開される変数
  FULL_URL: "https://${DOMAIN}/${PATH}"

# ジョブレベルの変数
deploy:
  variables:
    DEPLOY_ENV: production
  script:
    - deploy.sh

# Protected/Masked変数はGitLab UIで設定
# CI/CD > Variables で設定した変数は $SECRET_NAME で参照
```

### DAG (Directed Acyclic Graph)

```yaml
stages:
  - build
  - test
  - deploy

build_a:
  stage: build
  script: echo "Build A"

build_b:
  stage: build
  script: echo "Build B"

test_a:
  stage: test
  needs: [build_a]
  script: echo "Test A"

test_b:
  stage: test
  needs: [build_b]
  script: echo "Test B"

deploy:
  stage: deploy
  needs:
    - job: test_a
      artifacts: true
    - job: test_b
      artifacts: true
  script: echo "Deploy"
```
