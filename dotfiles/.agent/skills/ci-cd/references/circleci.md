# CircleCI リファレンス

最小構成例。orb / executor のバージョンは最新安定版を確認して選ぶ。

## 最小構成

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

## GitHub Actions との主な差分

- 再利用単位は orbs（GitHub Actions の action 相当）
- `jobs` を `workflows` で組み合わせ、job 間の依存は `requires` で宣言する
- 実行環境は `docker` / `machine` / `macos` の executor で指定する
- 設定の検証は `circleci config validate` で行う
