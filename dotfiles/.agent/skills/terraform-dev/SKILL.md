---
name: terraform-dev
description: "Use when the user asks to implement, refactor, validate, review, or troubleshoot Terraform/OpenTofu code, modules, providers, variables, state, plans, imports, security, or infrastructure changes."
---

# Terraform開発スキル

Terraformコードの実装、検証、リファクタリング、モジュール設計を効率的に行うためのガイド。

## 実装前の必須確認

**プロジェクト構成ファイルを必ず確認する。** Terraformバージョン、プロバイダ、バックエンド設定を把握する。

確認項目:
- `versions.tf` / `terraform.tf`: required_version, required_providers
- `backend.tf`: S3/GCS/Azure Blob等のバックエンド設定
- `.terraform-version`: tfenvで使用するバージョン
- `terragrunt.hcl`: Terragrunt使用時の設定
- `.tflint.hcl`: TFLint設定
- workspace / backend / state の場所、対象環境、production 影響
- `terraform plan` を安全に実行できる認証・変数・backend 初期化状態

`apply`、`destroy`、`import`、state 操作、production workspace への変更、リソース削除を含む plan は、対象環境・影響・戻し方を示してユーザー承認を取る。原則としてこの skill では plan までを標準にし、apply は明示依頼がある場合だけ行う。

### プロジェクト構造例

```
infrastructure/
├── environments/
│   ├── dev/
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── terraform.tfvars
│   ├── staging/
│   └── production/
├── modules/
│   ├── vpc/
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── ecs/
│   └── rds/
└── shared/
    └── backend.tf
```

## コーディング規約

### 基本スタイル

```hcl
# ローカル変数で共通値を定義
locals {
  common_tags = {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "terraform"
  }
}

# リソース定義
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(local.common_tags, {
    Name = "${var.project_name}-vpc"
  })
}

# データソース参照
data "aws_availability_zones" "available" {
  state = "available"
}

# 条件付きリソース作成
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? 1 : 0
  domain = "vpc"

  tags = local.common_tags
}
```

### 変数と出力

変数には `description` と `type` を必ず書き、取りうる値が限られるなら `validation` を付ける。秘匿値の出力には `sensitive = true` を付ける。

```hcl
# variables.tf
variable "environment" {
  description = "Environment name (dev, staging, production)"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "Environment must be dev, staging, or production."
  }
}

# outputs.tf
output "database_endpoint" {
  description = "RDS endpoint"
  value       = aws_db_instance.main.endpoint
  sensitive   = true
}
```

object 型・`optional()`・構造化出力などの詳細は [references/module-design.md](references/module-design.md) 参照。

## モジュール設計と HCL パターン

判断基準:

- 1 モジュール 1 責務。モジュール間は必要な値だけを input/output で受け渡す
- 同種リソースの繰り返しは、順序変更で置換が起きる `count` よりキーが安定する `for_each` を優先する
- `dynamic` はネストブロックの繰り返しにだけ使い、可読性が落ちるなら列挙する
- 別 state の値は `terraform_remote_state` か data source で参照し、ID をハードコードしない
- マルチリージョン・マルチアカウントは provider alias で明示する

最小例:

```hcl
# environments/production/main.tf
module "vpc" {
  source = "../../modules/vpc"

  name       = "${var.project_name}-${var.environment}"
  cidr_block = var.vpc_cidr
  tags       = local.common_tags
}

# for_each でリソースを動的に作成（aws_security_group.main は別途定義済みの前提）
resource "aws_security_group_rule" "ingress" {
  for_each = var.ingress_rules

  type              = "ingress"
  from_port         = each.value.from_port
  to_port           = each.value.to_port
  protocol          = each.value.protocol
  cidr_blocks       = each.value.cidr_blocks
  security_group_id = aws_security_group.main.id
}
```

モジュール実装・呼び出しの完全な例、`dynamic` ブロック、for 式は [references/module-design.md](references/module-design.md)、remote state 参照と provider alias は [references/provider-patterns.md](references/provider-patterns.md) 参照。

## セキュリティベストプラクティス

```hcl
# シークレット管理（AWS Secrets Manager連携）
data "aws_secretsmanager_secret_version" "db_credentials" {
  secret_id = var.db_secret_id
}

locals {
  db_credentials = jsondecode(data.aws_secretsmanager_secret_version.db_credentials.secret_string)
}

resource "aws_db_instance" "main" {
  identifier = "${var.project_name}-db"

  username = local.db_credentials.username
  password = local.db_credentials.password

  # tfstateにパスワードを保存しない
  lifecycle {
    ignore_changes = [password]
  }
}

# 暗号化の有効化
resource "aws_s3_bucket" "main" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_server_side_encryption_configuration" "main" {
  bucket = aws_s3_bucket.main.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_id
    }
    bucket_key_enabled = true
  }
}

# パブリックアクセスブロック
resource "aws_s3_bucket_public_access_block" "main" {
  bucket = aws_s3_bucket.main.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

## CL/PR 運用（eng-practices）

Small CL、Why の残し方などの共通原則は `eng-practices` スキル参照。Terraform 固有には以下を徹底する。

- **Blast radius を明示**: PR 本文に対象 workspace、影響リソース、`plan` 要約（add/change/destroy 件数）、特に destroy/replace の対象、権限変更、公開設定変更を必ず書く。
- **段階適用**: production の変更は dev → staging → production の順で適用し、CL を分けるか、同一 CL なら適用順を明記する。

## コード品質チェック

実装後に確認:
- `terraform fmt -recursive` でフォーマット
- `terraform validate` で構文チェック
- `terraform plan` で変更内容を確認
- `tflint` でベストプラクティス違反を検出
- `trivy config` / `checkov` でセキュリティスキャン（tfsec は開発終了し Trivy に統合済み。既存 CI に残っていれば移行を提案する）
- plan には add/change/destroy の件数、削除・置換リソース、権限変更、公開設定変更がないか確認する
- 最終報告には対象 workspace/backend、plan 要約、実行した検証、未実行の理由、承認が必要な操作を含める

### 推奨チェックコマンド

```bash
# フォーマットと検証
terraform fmt -recursive
terraform validate

# プラン確認
terraform plan -out=tfplan

# セキュリティスキャン
tflint --recursive
trivy config .
checkov -d .
```

## リファレンス

詳細なガイドは以下を参照:

- **プロバイダ別パターン**: [references/provider-patterns.md](references/provider-patterns.md)
- **モジュール設計ガイド**: [references/module-design.md](references/module-design.md)
- **よくあるエラーと対処法**: [references/troubleshooting.md](references/troubleshooting.md)
