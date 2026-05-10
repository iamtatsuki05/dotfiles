# agent-job-scheduler layout

`agent-job-scheduler` skill は次の責務で分けています。

- `SKILL.md`: skill 本体の使い方
- `apps/`: 実装と実行入口
- `assets/`: 叩き台ファイルやテンプレート
- `references/`: 運用上の補助資料
- `dotfiles/.agent/evals/agent-job-scheduler/`: Waza eval suite

## 実装配置方針

`apps/agent-job-scheduler/` は、この skill の canonical な app 実装です。理由は次の 2 点です。

1. skill 配下だけで完結して辿れること
2. 実装の置き場所と skill の参照先を一致させること
