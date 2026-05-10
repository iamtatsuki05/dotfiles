# agent-job-scheduler evals

Waza eval suite for the `agent-job-scheduler` skill.

- `eval.yaml`: mock executor smoke suite for repository-wide `waza-eval-all`
- `model.yaml`: model-backed suite for `waza-eval-model` and `waza-eval-cli-agents`
- `tasks/`: queue / retry scenarios that exercise the skill workflow
- `fixtures/`: captured scheduler status and failed-job output used by the tasks

Useful commands:

```bash
mise run waza-eval-codex -- --dry-run --suite dotfiles/.agent/evals/agent-job-scheduler/model.yaml
mise run waza-eval-codex -- --allow --suite dotfiles/.agent/evals/agent-job-scheduler/model.yaml
mise run waza-eval-model -- --allow
```
