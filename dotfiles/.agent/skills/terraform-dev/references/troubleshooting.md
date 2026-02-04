# よくあるエラーと対処法

## 目次

- [状態ファイル関連](#状態ファイル関連)
- [プロバイダ関連](#プロバイダ関連)
- [リソース関連](#リソース関連)
- [モジュール関連](#モジュール関連)
- [パフォーマンス](#パフォーマンス)

---

## 状態ファイル関連

### State lock エラー

```
Error: Error acquiring the state lock
```

**原因**: 別のプロセスがstate lockを保持している

**対処法**:
```bash
# ロック情報を確認
terraform force-unlock LOCK_ID

# S3バックエンドの場合、DynamoDBで確認
aws dynamodb scan --table-name terraform-locks
```

### State不整合

```
Error: Resource already exists
```

**対処法**:
```bash
# 既存リソースをstateにインポート
terraform import aws_instance.example i-1234567890abcdef0

# stateからリソースを削除（リソース自体は削除されない）
terraform state rm aws_instance.example

# state一覧を確認
terraform state list
```

### Stateファイルの移動

```hcl
# moved ブロックでリファクタリング
moved {
  from = aws_instance.old_name
  to   = aws_instance.new_name
}

moved {
  from = module.old_module.aws_vpc.main
  to   = module.new_module.aws_vpc.this
}
```

---

## プロバイダ関連

### 認証エラー

```
Error: error configuring Terraform AWS Provider: no valid credential sources
```

**対処法**:
```bash
# 環境変数を確認
echo $AWS_ACCESS_KEY_ID
echo $AWS_SECRET_ACCESS_KEY
echo $AWS_PROFILE

# プロファイル指定
export AWS_PROFILE=my-profile

# 認証情報の確認
aws sts get-caller-identity
```

### プロバイダバージョンの競合

```
Error: Failed to query available provider packages
```

**対処法**:
```bash
# ロックファイルを更新
terraform init -upgrade

# 特定プラットフォーム用にロックファイル更新
terraform providers lock -platform=linux_amd64 -platform=darwin_amd64
```

### プロバイダのミラーリング

```bash
# オフライン環境用にプロバイダをダウンロード
terraform providers mirror /path/to/mirror

# ミラーから使用
export TF_PLUGIN_CACHE_DIR=/path/to/mirror
```

---

## リソース関連

### 依存関係エラー

```
Error: Error creating X: Y is not ready
```

**対処法**:
```hcl
# 明示的な依存関係を追加
resource "aws_ecs_service" "main" {
  # ...

  depends_on = [
    aws_lb_listener.https,
    aws_iam_role_policy_attachment.ecs_task
  ]
}
```

### リソース置換の回避

```hcl
resource "aws_instance" "main" {
  # ...

  lifecycle {
    # 特定属性の変更を無視
    ignore_changes = [
      ami,
      tags["LastUpdated"]
    ]

    # 置換前に新リソースを作成
    create_before_destroy = true

    # 削除を防止
    prevent_destroy = true
  }
}
```

### タイムアウトエラー

```
Error: timeout while waiting for state to become 'available'
```

**対処法**:
```hcl
resource "aws_db_instance" "main" {
  # ...

  timeouts {
    create = "60m"
    update = "60m"
    delete = "60m"
  }
}
```

### リソースのターゲット指定

```bash
# 特定リソースのみ操作
terraform plan -target=aws_instance.main
terraform apply -target=module.vpc

# 複数ターゲット
terraform apply -target=aws_instance.web -target=aws_instance.api
```

---

## モジュール関連

### モジュールソースエラー

```
Error: Failed to download module
```

**対処法**:
```bash
# Gitの認証確認（プライベートリポジトリ）
git config --global credential.helper store

# SSH鍵の確認
ssh -T git@github.com

# モジュールを再取得
terraform get -update
```

### 循環依存

```
Error: Cycle: module.a, module.b
```

**対処法**:
```hcl
# モジュール設計を見直し、共通の値はルートで管理
locals {
  vpc_id = module.vpc.vpc_id
}

module "ecs" {
  source = "./modules/ecs"
  vpc_id = local.vpc_id
}

module "rds" {
  source = "./modules/rds"
  vpc_id = local.vpc_id
}
```

### Output参照エラー

```
Error: Unsupported attribute
```

**対処法**:
```bash
# モジュールのoutputを確認
terraform output
terraform output -module=vpc

# outputが定義されているか確認
cat modules/vpc/outputs.tf
```

---

## パフォーマンス

### Plan/Applyが遅い

```bash
# 並列処理数を増加（デフォルト10）
terraform apply -parallelism=20

# 特定リソースのみ操作
terraform plan -target=module.specific
```

### 大量リソースの管理

```hcl
# count よりも for_each を使用（stateの安定性向上）
resource "aws_instance" "main" {
  for_each = var.instances

  ami           = each.value.ami
  instance_type = each.value.type

  tags = {
    Name = each.key
  }
}
```

### State操作の高速化

```bash
# リモートstateのローカルキャッシュ
terraform init -backend-config="skip_s3_backend_cache=true"

# 部分的なstate操作
terraform state pull > state.json
terraform state push state.json
```

---

## デバッグ

### ログ出力

```bash
# デバッグログを有効化
export TF_LOG=DEBUG
export TF_LOG_PATH=./terraform.log

# 特定コンポーネントのログ
export TF_LOG_CORE=TRACE
export TF_LOG_PROVIDER=DEBUG
```

### Planの詳細表示

```bash
# JSON形式で出力
terraform plan -out=tfplan
terraform show -json tfplan > plan.json

# 変更内容を詳細表示
terraform plan -detailed-exitcode
```

### プロバイダのデバッグ

```bash
# クラッシュログの確認
cat crash.log

# プロバイダのソースコードを確認（オープンソースの場合）
# https://github.com/hashicorp/terraform-provider-aws
```
