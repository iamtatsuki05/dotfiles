# Shared Hooks

English version: [README.md](README.md)

このディレクトリは、複数の local AI agent で共有する hook script を置く場所です。
`dotfiles/.agent/sync.sh` により、agent ごとの hook location へ symlink されます。

## Hooks

| Hook | 用途 |
|---|---|
| `agent_context_reminder.sh` | 対応 agent の prompt / session hook で、この repo 向け reminder context を出力する。 |
| `agent_turn_done_notify.sh` | turn 完了通知に対応する agent で使う共有完了音を鳴らす。 |
| `jupytext_sync.sh` | agent が paired `.py` を編集したあと、対応する Jupyter notebook を同期する。 |

agent 固有の hook 登録は `../apps/` 配下にあります。
JSON hook map を読む agent と、hook directory の shell script を読む agent があります。

## 更新ルール

- 複数 agent で共有する hook は tool-agnostic に保ちます。
- hook script に secret を置きません。
- 編集後は shell syntax を検証します。
- hook 出力を変更した場合は、script 本体と呼び出し元の agent config を両方確認します。
- hook が agent 由来の JSON を読む場合は、代表 payload を `python3 -m json.tool` に通して確認します。

## よく使う確認コマンド

```bash
bash -n dotfiles/.agent/hooks/agent_context_reminder.sh
bash -n dotfiles/.agent/hooks/agent_turn_done_notify.sh
bash -n dotfiles/.agent/hooks/jupytext_sync.sh
printf '{}' | dotfiles/.agent/hooks/agent_context_reminder.sh | python3 -m json.tool
zsh tests/test_agent_sync.sh
```
