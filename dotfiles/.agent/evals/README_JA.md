# Waza Eval Suites

English version: [README.md](README.md)

このディレクトリは、`../skills/` 配下の skill 向け Waza eval suite を置く場所です。
各 suite は、skill に期待する振る舞いを小さく review 可能にした contract です。

## Suite 構成

多くの suite は次の形です。

```text
evals/<skill>/
├── eval.yaml       # mock executor smoke suite
├── model.yaml      # model-backed quality suite
├── tasks/*.yaml    # individual scenarios
└── fixtures/       # optional input files or captured logs
```

`eval.yaml` は repo-wide の軽い smoke check 向けです。
`model.yaml` は model-backed evaluation 向けで、executor によっては外部 credential や network が必要です。

## 現在の suite

| Suite | 対象 |
|---|---|
| `agent-job-scheduler` | scheduler skill の queue / recovery workflow。 |
| `alphaxiv-paper-lookup` | 論文 lookup と要約。 |
| `api-design` | API review と設計 feedback。 |
| `auto-debugger` | stack trace や失敗コードからの debugging。 |
| `ci-cd` | CI workflow review。 |
| `claude-code` | Claude Code 相談 routing。 |
| `codex` | Codex 相談 routing。 |
| `colab-mcp` | Colab MCP の安全な setup / troubleshoot。 |
| `database-dev` | database diagnosis と query review。 |
| `empirical-prompt-tuning` | prompt tuning workflow の品質。 |
| `go-dev` | Go debugging / review。 |
| `goal-prompt-builder` | `/goal` prompt 作成、言語維持、拒否挙動。 |
| `gws` | Google Workspace CLI inspection。 |
| `magika` | file type identification。 |
| `markdown-docs` | Markdown review、proofread、restructure。 |
| `markitdown` | Markdown conversion routing。 |
| `missing-tools` | command 不在時の安全な fallback plan。 |
| `pr-code-review` | PR review finding。 |
| `prompt-tuner` | prompt 改善出力。 |
| `python-dev` | Python debugging と typing。 |
| `retrospective-codify` | 繰り返し学習を durable rule にする workflow。 |
| `security-check` | security review finding。 |
| `superpowers-*` | vendored Superpowers workflow skill。 |
| `terraform-dev` | Terraform plan / module review。 |
| `typescript-dev` | TypeScript / Zod debugging。 |

## suite 追加・更新

- skill または workflow 名のディレクトリを追加します。
- `eval.yaml` は可能な限り軽く deterministic にします。
- 主観品質、policy adherence、tool routing を見たい場合は `model.yaml` を追加します。
- test prompt は `tasks/*.yaml` に置きます。
- 再利用する入力は `fixtures/` に置きます。
- 生成された結果ディレクトリは commit しません。Waza の結果は `.waza-results/` に出ます。

## よく使う確認コマンド

```bash
mise run waza-check
mise run waza-eval -- --dry-run
mise run waza-eval-model -- --dry-run --suite dotfiles/.agent/evals/<skill>/model.yaml
zsh scripts/waza_eval_cli_agent.sh codex --dry-run --suite dotfiles/.agent/evals/<skill>/model.yaml
```

選択した agent / model executor を本当に実行する場合だけ `--allow` を使います。
