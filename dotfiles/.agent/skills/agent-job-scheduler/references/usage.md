# agent-job-scheduler usage

`agent-job-scheduler` skill から使うときの最短リファレンスです。

## 主要パス

- skill パッケージ: [agent-job-scheduler](/Users/tatsuki/src/dotfiles/dotfiles/.agent/skills/agent-job-scheduler)
- app 実装: [apps/agent-job-scheduler](/Users/tatsuki/src/dotfiles/dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler)

## よく使うコマンド

```bash
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler status
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler enqueue --agent codex --workdir /abs/path --prompt-file dotfiles/.agent/skills/agent-job-scheduler/assets/examples/prompt.codex.txt
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler run-once
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler active-runs
dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler show-config
```

`--agent` は `claude`、`codex`、`copilot`、`cursor`、`devin`、`gemini`、`hermes`、`opencode` を指定できます。

## 使い分け

- `apps/`: 実行可能な app 実装
- `assets/`: サンプル CSV や prompt テンプレート
- `references/`: 運用メモと参照資料
