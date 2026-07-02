# Waza Eval Suites

Japanese version: [README_JA.md](README_JA.md)

This directory contains Waza eval suites for the skills under `../skills/`.
Each suite is a small, reviewable contract for a skill's expected behavior.

## Suite Layout

Most suites use this shape:

```text
evals/<skill>/
├── eval.yaml       # mock executor smoke suite
├── model.yaml      # model-backed quality suite
├── tasks/*.yaml    # individual scenarios
└── fixtures/       # optional input files or captured logs
```

`eval.yaml` is intended for cheap repository-wide smoke checks.
`model.yaml` is intended for model-backed evaluation and may require external credentials or network access depending on the executor.

## Current Suites

| Suite | Covers |
|---|---|
| `agent-cli-consult` | External agent CLI (Codex / Claude Code) consultation routing. |
| `agent-job-scheduler` | Queueing and recovery workflows for the scheduler skill. |
| `alphaxiv-paper-lookup` | Paper lookup and summarization behavior. |
| `api-design` | API review and design feedback. |
| `auto-debugger` | Debugging from stack traces and failing code. |
| `ci-cd` | CI workflow review. |
| `colab-mcp` | Safe Colab MCP setup and troubleshooting. |
| `compatibility-safety` | Compatibility, alias, and fallback discipline. |
| `database-dev` | Database diagnosis and query review. |
| `empirical-prompt-tuning` | Prompt tuning workflow quality. |
| `go-dev` | Go debugging and review behavior. |
| `goal-prompt-builder` | `/goal` prompt construction, language handling, and refusal behavior. |
| `gws` | Google Workspace CLI inspection behavior. |
| `magika` | File type identification. |
| `markdown-docs` | Markdown review, proofreading, and restructuring. |
| `markitdown` | Conversion-to-Markdown routing. |
| `missing-tools` | Safe fallback planning when a command is unavailable. |
| `pr-code-review` | PR review findings. |
| `prompt-tuner` | Prompt improvement output. |
| `python-dev` | Python debugging and typing behavior. |
| `retrospective-codify` | Turning repeated lessons into durable rules. |
| `security-check` | Security review findings. |
| `superpowers-*` | Vendored Superpowers workflow skills. |
| `terraform-dev` | Terraform plan and module review. |
| `typescript-dev` | TypeScript / Zod debugging. |

## Add Or Update A Suite

- Add a directory named after the skill or workflow.
- Keep `eval.yaml` cheap and deterministic where possible.
- Add `model.yaml` when subjective quality, policy adherence, or tool-routing behavior needs model-backed evaluation.
- Put test prompts in `tasks/*.yaml`.
- Put reusable inputs under `fixtures/`.
- Do not commit generated result directories; Waza writes results under `.waza-results/`.

## Common Checks

```bash
mise run waza-check
mise run waza-eval -- --dry-run
mise run waza-eval-model -- --dry-run --suite dotfiles/.agent/evals/<skill>/model.yaml
zsh scripts/waza_eval_cli_agent.sh codex --dry-run --suite dotfiles/.agent/evals/<skill>/model.yaml
```

Use `--allow` only when you intentionally want to run the selected agent or model executor.
