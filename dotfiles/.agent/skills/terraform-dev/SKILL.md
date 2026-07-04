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

### プロジェクト構造

既存プロジェクトでは既存のディレクトリ構成と分割方針を優先する。新規なら `environments/<env>/`（env ごとの root module）+ `modules/<name>/` の構成を基本とする。モジュール内のファイル分割や大規模構成の例は [references/module-design.md](references/module-design.md) の「ディレクトリ構成」を参照。

## コーディング規約

### 基本スタイル

- 共通タグや共通値は `locals` に一元化し、リソース側で `merge()` する。AWS なら provider の `default_tags` も検討する。
- 環境や既存リソースへの依存は ID のハードコードでなく data source で参照する。
- 条件付きリソース作成は `count = 条件 ? 1 : 0` を使う（ただし後から `for_each` に変えると置換が起きる点に注意）。

実例は [references/module-design.md](references/module-design.md) と [references/provider-patterns.md](references/provider-patterns.md) を参照。

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

- シークレットは `.tf` / `.tfvars` に平文で書かず、Secrets Manager / SSM Parameter Store の data source 参照か、Terraform 1.10+ の ephemeral values を使う。
- **`lifecycle { ignore_changes = [password] }` は差分検出を止めるだけで、値は state に平文で保存される。** state に入る秘匿値対策としては不十分なので、state 自体の保護（S3 backend の `encrypt = true`、state バケットへのアクセス制御）と ephemeral / secrets manager 参照を優先する。
- ストレージ（S3 等）は SSE-KMS による暗号化とパブリックアクセスブロックを既定とする。
- 秘匿値を含む variable / output には `sensitive = true` を付ける（これも state への平文保存は防がない）。

Secrets Manager 連携・S3 暗号化・パブリックアクセスブロックの実装例は [references/provider-patterns.md](references/provider-patterns.md) の AWS 節を参照。

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
- `tflint` / `trivy` / `checkov` が未導入の場合は `missing-tools` skill で一時実行する
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
