---
name: terraform-dev
description: "Use when the user asks to implement, refactor, validate, review, or troubleshoot Terraform/OpenTofu code, modules, providers, variables, state, plans, imports, or infrastructure security. Read once per session. Do not use for cloud CLI/console operations, Kubernetes manifests without a Terraform provider, or CI YAML that only runs terraform."
---

# Terraform 開発スキル

Terraform / OpenTofu の実装、検証、リファクタリング、モジュール設計を、対象の backend と環境の制約に合わせて進めるための手順と判断基準。既定は `plan` までで、`apply` は明示依頼がある場合だけ行う。

この skill は session につき 1 回だけ読む。compaction 後は `checkpoint.md` の「適用中の skill と解決済みの実行形」を見て続け、SKILL.md を読み直さない。`$terraform-dev` で本文が注入済みの場合も再読しない。

## 着手前の確認

次の順に読み、決めた内容を `checkpoint.md` に 1〜2 行で残す。repo が無く相談だけの依頼では設定ファイルを探さず、前提にしたバージョンや設定を回答に明記する。

1. `versions.tf` / `terraform.tf`: `required_version` と `required_providers`。`.terraform-version` / `.tool-versions` があれば tfenv / mise のバージョン。`import` ブロック(1.5+)、`moved`(1.1+)、`removed`(1.7+)、ephemeral values(1.10+)は対象バージョンが対応する場合だけ使う。
2. `backend.tf` と workspace: state の場所(S3 / GCS / Azure Blob / Terraform Cloud)、`terraform workspace show`、対象環境(dev / staging / production)。`terragrunt.hcl` があれば実行形は `terragrunt` に合わせる。
3. `.tflint.hcl`、`.pre-commit-config.yaml`、CI の設定: fmt / validate / lint / scan の実行形。あればそれに従う。
4. `plan` を安全に実行できるか: 認証(環境変数、profile、assume role)、`*.tfvars` の所在、`terraform init` 済みか。認証が無ければ `validate` までにとどめ、その旨を報告する。
5. 既存のディレクトリ構成と分割方針(`environments/<env>/` + `modules/<name>/` か否か)。既存構成はこの skill の既定より優先する。

`apply`、`destroy`、`terraform import`、`state mv` / `state rm`、production workspace への変更、削除・置換を含む plan の適用は、対象環境、影響、戻し方を示してユーザー承認を取る。承認前に実行しない。`import` / `moved` ブロックの `plan` による差分確認は承認不要で、state に反映する `apply` が承認対象。

## 進め方

1. 変更を書いたら `terraform fmt -recursive` → `terraform validate` → `terraform plan -out=tfplan` の順に回し、plan の add / change / destroy 件数と destroy / replace 対象を読む。`tfplan` には sensitive 値が平文で入るので、`.gitignore` 済みか確認し共有しない。意図しない replace は `terraform plan` の `# forces replacement` 行から原因を特定する。
2. リソース名や module の移動は `moved` ブロックで state を追従させ、`state mv` の手作業を避ける。既存リソースの取り込みは `import` ブロック(1.5+)を優先し、旧バージョンだけ `terraform import`。state lock、不整合、置換の回避策は [references/troubleshooting.md](references/troubleshooting.md) を見る。
3. 互換のための変数 alias(新旧名を両方受ける)、default 値補完、legacy の分岐は、移行計画と削除予定がある場合だけ追加する。実際に書きそうになった時点でだけ `compatibility-safety` を読む。
4. 検証は `terraform test`(1.6+、`tests/*.tftest.hcl`)か既存の Terratest に合わせる。テスト基盤が無い場合は `plan` の結果を検証の代わりにし、その旨を報告する。

## 規約と判断基準

プロジェクトに規約や既存パターンがあればそれに従う。無い場合の既定:

HCL:

- 変数には `description` と `type` を必ず書き、取りうる値が限られるなら `validation` を付ける。秘匿値の variable / output には `sensitive = true`。object 型と `optional()`、構造化出力は [references/module-design.md](references/module-design.md) の「変数設計」「出力設計」。
- 共通タグや共通値は `locals` に一元化し、リソース側で `merge()` する。AWS なら provider の `default_tags` も候補。
- 同種リソースの繰り返しは、順序変更で置換が起きる `count` よりキーが安定する `for_each` を優先する。条件付き作成だけ `count = 条件 ? 1 : 0`(後から `for_each` に変えると置換が起きる)。`dynamic` はネストブロックの繰り返しにだけ使う。
- 既存リソースや別 state の値は ID をハードコードせず、data source か `terraform_remote_state` で参照する。マルチリージョン・マルチアカウントは provider alias で明示する。実例は [references/provider-patterns.md](references/provider-patterns.md) の「プロバイダ共通パターン」。

モジュール:

- 1 モジュール 1 責務。モジュール間は必要な値だけを input / output で受け渡し、モジュール全体を渡さない。
- 新規の構成は `environments/<env>/`(env ごとの root module)+ `modules/<name>/`。モジュールの実装・呼び出しの完全な例は [references/module-design.md](references/module-design.md) の「モジュール実装と呼び出し例」。
- 外部モジュールは `version` か git の `ref` で固定する。

セキュリティ:

- シークレットは `.tf` / `.tfvars` に平文で書かず、Secrets Manager / SSM Parameter Store の data source 参照か ephemeral values(1.10+)を使う。
- `lifecycle { ignore_changes = [password] }` と `sensitive = true` は差分検出や表示を止めるだけで、値は state に平文で残る。state 自体の保護(S3 backend の `encrypt = true`、state バケットのアクセス制御)を優先する。
- ストレージ(S3 など)は SSE-KMS と public access block を既定にし、public ACL は明示要件がある場合だけ。実装例は [references/provider-patterns.md](references/provider-patterns.md) の AWS 節。
- 手動で作られたリソースや手動 import は、`import` ブロックでコード化し、plan に差分が出ないところまで揃えてから扱う。

## 完了前の確認

決めた実行形で次を実行する(Terragrunt なら `terragrunt` に置き換える)。

```bash
terraform fmt -recursive -check
terraform validate
terraform plan -out=tfplan    # 認証と init が整っている場合
tflint --recursive            # .tflint.hcl がある場合
trivy config .                # または checkov -d .
```

- `tflint` / `trivy` / `checkov` が未導入なら `missing-tools` を読んで一時実行する。tfsec は Trivy に統合済みなので、CI に残っていれば移行を提案する。
- plan の add / change / destroy 件数、削除・置換対象、IAM や権限の変更、公開設定の変更を確認した。
- 実行できない検証(認証なし、backend 未初期化など)は、コマンド、理由、残るリスクを報告に回す。

## 報告に含めること

- 対象 workspace / backend / 環境と、変更したリソース・モジュール。
- plan の要約(add / change / destroy 件数、destroy / replace 対象、権限変更、公開設定変更)。production の変更は dev → staging → production の適用順。
- 実行した検証コマンドと結果。未実行の検証とその理由。
- 承認が必要な操作(`apply`、`destroy`、`terraform import`、state 操作)とその状態。
- PR description を書く段階になったら `eng-practices` を読む。それ以外の場面では読まない。
