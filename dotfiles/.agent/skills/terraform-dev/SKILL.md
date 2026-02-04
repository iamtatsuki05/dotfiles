---
name: terraform-dev
description: Terraform/OpenTofuによるインフラストラクチャ開発のための汎用スキル。IaCコード実装、リファクタリング、モジュール作成、プランレビュー、セキュリティ改善を支援。Terraformファイル(.tf/.tfvars)の作成・編集、terraform plan/applyによる検証、状態管理、マルチクラウド対応、ベストプラクティス適用時に使用。
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

### 変数定義

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

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "Must be a valid CIDR block."
  }
}

variable "instance_config" {
  description = "EC2 instance configuration"
  type = object({
    instance_type = string
    volume_size   = number
    enable_monitoring = optional(bool, true)
  })
}

variable "subnet_cidrs" {
  description = "List of subnet CIDR blocks"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags for resources"
  type        = map(string)
  default     = {}
}
```

### 出力定義

```hcl
# outputs.tf
output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "List of private subnet IDs"
  value       = aws_subnet.private[*].id
}

output "database_endpoint" {
  description = "RDS endpoint"
  value       = aws_db_instance.main.endpoint
  sensitive   = true
}
```

## モジュール設計

### 再利用可能なモジュール

```hcl
# modules/vpc/main.tf
resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_hostnames = var.enable_dns_hostnames
  enable_dns_support   = var.enable_dns_support

  tags = merge(var.tags, {
    Name = var.name
  })
}

resource "aws_subnet" "public" {
  count = length(var.public_subnet_cidrs)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index]
  map_public_ip_on_launch = true

  tags = merge(var.tags, {
    Name = "${var.name}-public-${count.index + 1}"
    Type = "public"
  })
}

resource "aws_subnet" "private" {
  count = length(var.private_subnet_cidrs)

  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index]

  tags = merge(var.tags, {
    Name = "${var.name}-private-${count.index + 1}"
    Type = "private"
  })
}
```

### モジュール呼び出し

```hcl
# environments/production/main.tf
module "vpc" {
  source = "../../modules/vpc"

  name       = "${var.project_name}-${var.environment}"
  cidr_block = var.vpc_cidr

  public_subnet_cidrs  = var.public_subnet_cidrs
  private_subnet_cidrs = var.private_subnet_cidrs
  availability_zones   = data.aws_availability_zones.available.names

  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = local.common_tags
}

module "ecs_cluster" {
  source = "../../modules/ecs"

  cluster_name = "${var.project_name}-${var.environment}"
  vpc_id       = module.vpc.vpc_id
  subnet_ids   = module.vpc.private_subnet_ids

  depends_on = [module.vpc]
}
```

## 高度なパターン

### for_each と dynamic ブロック

```hcl
# for_each でリソースを動的に作成
resource "aws_security_group_rule" "ingress" {
  for_each = var.ingress_rules

  type              = "ingress"
  from_port         = each.value.from_port
  to_port           = each.value.to_port
  protocol          = each.value.protocol
  cidr_blocks       = each.value.cidr_blocks
  security_group_id = aws_security_group.main.id
  description       = each.value.description
}

# dynamic ブロック
resource "aws_security_group" "main" {
  name   = "${var.name}-sg"
  vpc_id = var.vpc_id

  dynamic "ingress" {
    for_each = var.ingress_rules
    content {
      from_port   = ingress.value.from_port
      to_port     = ingress.value.to_port
      protocol    = ingress.value.protocol
      cidr_blocks = ingress.value.cidr_blocks
      description = ingress.value.description
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
```

### 条件分岐とループ

```hcl
# 三項演算子
resource "aws_instance" "main" {
  ami           = var.ami_id
  instance_type = var.environment == "production" ? "m5.large" : "t3.small"

  monitoring = var.environment == "production" ? true : false
}

# for式
locals {
  subnet_map = {
    for idx, cidr in var.subnet_cidrs :
    "subnet-${idx}" => {
      cidr = cidr
      az   = var.availability_zones[idx % length(var.availability_zones)]
    }
  }

  # フィルタリング
  production_instances = [
    for instance in var.instances :
    instance if instance.environment == "production"
  ]
}
```

### データソースとリモートステート

```hcl
# リモートステート参照
data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "terraform-state-bucket"
    key    = "network/terraform.tfstate"
    region = "ap-northeast-1"
  }
}

# 既存リソースのインポート用データソース
data "aws_vpc" "existing" {
  filter {
    name   = "tag:Name"
    values = ["existing-vpc"]
  }
}
```

### プロバイダエイリアス

```hcl
# マルチリージョン対応
provider "aws" {
  region = "ap-northeast-1"
  alias  = "tokyo"
}

provider "aws" {
  region = "us-east-1"
  alias  = "virginia"
}

resource "aws_s3_bucket" "replica" {
  provider = aws.virginia
  bucket   = "${var.bucket_name}-replica"
}
```

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

## コード品質チェック

実装後に確認:
- `terraform fmt -recursive` でフォーマット
- `terraform validate` で構文チェック
- `terraform plan` で変更内容を確認
- `tflint` でベストプラクティス違反を検出
- `checkov` / `tfsec` でセキュリティスキャン

### 推奨チェックコマンド

```bash
# フォーマットと検証
terraform fmt -recursive
terraform validate

# プラン確認
terraform plan -out=tfplan

# セキュリティスキャン
tflint --recursive
tfsec .
checkov -d .
```

## リファレンス

詳細なガイドは以下を参照:

- **プロバイダ別パターン**: [references/provider-patterns.md](references/provider-patterns.md)
- **モジュール設計ガイド**: [references/module-design.md](references/module-design.md)
- **よくあるエラーと対処法**: [references/troubleshooting.md](references/troubleshooting.md)
