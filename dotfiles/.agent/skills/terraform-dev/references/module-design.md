# モジュール設計ガイド

## 目次

- [モジュール設計原則](#モジュール設計原則)
- [ディレクトリ構成](#ディレクトリ構成)
- [変数設計](#変数設計)
- [出力設計](#出力設計)
- [バージョニング](#バージョニング)
- [テスト](#テスト)

---

## モジュール設計原則

### 単一責任の原則

1つのモジュールは1つの責任を持つ。

```
# Good: 責任が明確
modules/
├── vpc/           # VPCとサブネットのみ
├── security-group/ # セキュリティグループのみ
├── alb/           # ALBのみ
└── ecs/           # ECSクラスタとサービス

# Bad: 責任が曖昧
modules/
└── infrastructure/  # VPC、SG、ALB、ECS全部入り
```

### 疎結合・高凝集

```hcl
# Good: モジュール間は最小限のインターフェースで接続
module "vpc" {
  source = "./modules/vpc"
  # ...
}

module "ecs" {
  source = "./modules/ecs"

  vpc_id     = module.vpc.vpc_id      # 必要な値のみ受け取る
  subnet_ids = module.vpc.private_subnet_ids
}

# Bad: モジュール全体を渡す
module "ecs" {
  source = "./modules/ecs"

  vpc = module.vpc  # 不要な依存が発生
}
```

### 合成可能性

小さなモジュールを組み合わせて大きな構成を作る。

```hcl
# 基礎モジュール
module "vpc" {
  source = "./modules/vpc"
}

module "security_groups" {
  source = "./modules/security-groups"
  vpc_id = module.vpc.vpc_id
}

# 合成モジュール（オプション）
module "network" {
  source = "./modules/network"  # vpc + security-groups を内包
}
```

---

## ディレクトリ構成

### 標準的なモジュール構成

```
modules/
└── module-name/
    ├── main.tf           # メインリソース定義
    ├── variables.tf      # 入力変数
    ├── outputs.tf        # 出力値
    ├── versions.tf       # プロバイダ・Terraformバージョン
    ├── locals.tf         # ローカル値（オプション）
    ├── data.tf           # データソース（オプション）
    └── README.md         # モジュールドキュメント
```

### 大規模モジュール構成

```
modules/
└── ecs-service/
    ├── main.tf
    ├── iam.tf            # IAMリソース
    ├── monitoring.tf     # CloudWatch等
    ├── variables.tf
    ├── outputs.tf
    ├── versions.tf
    ├── examples/
    │   ├── basic/
    │   │   └── main.tf
    │   └── with-alb/
    │       └── main.tf
    └── tests/
        └── basic_test.go
```

---

## 変数設計

### 命名規則

```hcl
# リソース識別子
variable "name" {
  description = "Name prefix for all resources"
  type        = string
}

# 機能フラグ
variable "enable_encryption" {
  description = "Enable encryption at rest"
  type        = bool
  default     = true
}

# 設定値
variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.micro"
}

# リスト/マップ
variable "subnet_ids" {
  description = "List of subnet IDs"
  type        = list(string)
}

variable "tags" {
  description = "Additional tags for resources"
  type        = map(string)
  default     = {}
}
```

### 必須 vs オプション

```hcl
# 必須変数: default なし
variable "vpc_id" {
  description = "VPC ID where resources will be created"
  type        = string
}

# オプション変数: default あり
variable "log_retention_days" {
  description = "CloudWatch Logs retention period"
  type        = number
  default     = 30
}

# オプション（nullable）
variable "kms_key_id" {
  description = "KMS key ID for encryption (uses AWS managed key if null)"
  type        = string
  default     = null
}
```

### 複雑な型定義

```hcl
variable "container_definitions" {
  description = "List of container definitions"
  type = list(object({
    name      = string
    image     = string
    cpu       = number
    memory    = number
    essential = optional(bool, true)
    port_mappings = optional(list(object({
      container_port = number
      host_port      = optional(number)
      protocol       = optional(string, "tcp")
    })), [])
    environment = optional(map(string), {})
    secrets     = optional(map(string), {})
  }))
}

variable "autoscaling_config" {
  description = "Auto scaling configuration"
  type = object({
    min_capacity       = number
    max_capacity       = number
    target_cpu_percent = optional(number, 70)
    scale_in_cooldown  = optional(number, 300)
    scale_out_cooldown = optional(number, 60)
  })
  default = null
}
```

### バリデーション

```hcl
variable "environment" {
  description = "Environment name"
  type        = string

  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "Environment must be one of: dev, staging, production."
  }
}

variable "instance_count" {
  description = "Number of instances"
  type        = number

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 10
    error_message = "Instance count must be between 1 and 10."
  }
}

variable "cidr_block" {
  description = "CIDR block for the VPC"
  type        = string

  validation {
    condition     = can(cidrhost(var.cidr_block, 0))
    error_message = "Must be a valid CIDR block."
  }
}
```

---

## 出力設計

### 基本原則

```hcl
# リソースID
output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.this.id
}

# ARN
output "role_arn" {
  description = "ARN of the IAM role"
  value       = aws_iam_role.this.arn
}

# 複数リソースのID
output "private_subnet_ids" {
  description = "List of private subnet IDs"
  value       = aws_subnet.private[*].id
}

# センシティブな値
output "database_password" {
  description = "Database master password"
  value       = random_password.db.result
  sensitive   = true
}
```

### 構造化出力

```hcl
# オブジェクトとして出力
output "cluster" {
  description = "ECS cluster details"
  value = {
    id   = aws_ecs_cluster.this.id
    arn  = aws_ecs_cluster.this.arn
    name = aws_ecs_cluster.this.name
  }
}

# マップとして出力
output "subnets" {
  description = "Map of subnet details by type"
  value = {
    public = {
      ids  = aws_subnet.public[*].id
      arns = aws_subnet.public[*].arn
    }
    private = {
      ids  = aws_subnet.private[*].id
      arns = aws_subnet.private[*].arn
    }
  }
}
```

---

## バージョニング

### セマンティックバージョニング

```
MAJOR.MINOR.PATCH

MAJOR: 破壊的変更（変数削除、リソース名変更等）
MINOR: 後方互換性のある機能追加
PATCH: バグ修正
```

### Gitタグによるバージョン管理

```hcl
# バージョン指定での使用
module "vpc" {
  source  = "git::https://github.com/org/terraform-modules.git//vpc?ref=v1.2.0"
}

# Terraform Registry
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"
}
```

### 破壊的変更の回避

```hcl
# 変数名変更時は両方サポート
variable "name" {
  description = "Name prefix (deprecated: use name_prefix instead)"
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Name prefix for resources"
  type        = string
  default     = null
}

locals {
  name_prefix = coalesce(var.name_prefix, var.name, "default")
}
```

---

## テスト

### Terratest（Go）

```go
package test

import (
    "testing"

    "github.com/gruntwork-io/terratest/modules/terraform"
    "github.com/stretchr/testify/assert"
)

func TestVpcModule(t *testing.T) {
    t.Parallel()

    terraformOptions := terraform.WithDefaultRetryableErrors(t, &terraform.Options{
        TerraformDir: "../examples/basic",
        Vars: map[string]interface{}{
            "name":       "test-vpc",
            "cidr_block": "10.0.0.0/16",
        },
    })

    defer terraform.Destroy(t, terraformOptions)

    terraform.InitAndApply(t, terraformOptions)

    vpcId := terraform.Output(t, terraformOptions, "vpc_id")
    assert.NotEmpty(t, vpcId)
}
```

### terraform test（Terraform 1.6+）

```hcl
# tests/basic.tftest.hcl
run "create_vpc" {
  command = apply

  variables {
    name       = "test-vpc"
    cidr_block = "10.0.0.0/16"
  }

  assert {
    condition     = aws_vpc.this.cidr_block == "10.0.0.0/16"
    error_message = "VPC CIDR block mismatch"
  }

  assert {
    condition     = length(aws_subnet.private) == 3
    error_message = "Expected 3 private subnets"
  }
}

run "validate_outputs" {
  command = plan

  variables {
    name       = "test-vpc"
    cidr_block = "10.0.0.0/16"
  }

  assert {
    condition     = output.vpc_id != ""
    error_message = "VPC ID should not be empty"
  }
}
```

### terraform-docs

```bash
# README.md の自動生成
terraform-docs markdown table --output-file README.md ./
```

```hcl
# .terraform-docs.yml
formatter: markdown table

output:
  file: README.md
  mode: inject

sort:
  enabled: true
  by: required

settings:
  indent: 2
  escape: true
```
