# agent-job-scheduler usage

`agent-job-scheduler` skill から使うときの最短リファレンスです。コマンドの基本形と一覧は [SKILL.md](../SKILL.md) の「コマンド例」を参照してください。

## SKILL.md にない固有情報

```bash
AJS=dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler/bin/agent-job-scheduler

# prompt をファイルで渡す（assets/examples/ のテンプレートを叩き台にできる）
"$AJS" enqueue --agent codex --workdir /abs/path \
  --prompt-file dotfiles/.agent/skills/agent-job-scheduler/assets/examples/prompt.codex.txt
```

`--agent` は `antigravity`、`claude`、`codex`、`copilot`、`cursor`、`devin`、`hermes`、`opencode`、`openclaw`、`grok` を指定できます。
