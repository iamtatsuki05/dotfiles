# Agent App Configs

English version: [README.md](README.md)

このディレクトリは、各 local AI CLI agent 向けの個別設定を置く場所です。
`dotfiles/.agent/sync.sh` により、それぞれの agent home へ同期されます。

共通の振る舞い prompt は `../AGENTS.md` にあります。
このディレクトリでは、tool 固有の設定、MCP、hooks、permission、plugin、runtime 連携を管理します。

## 構成

| Path | Agent | 内容 |
|---|---|---|
| `antigravity-cli/` | Antigravity CLI | Gemini / Antigravity 向け設定と `dotfiles-agent` plugin。 |
| `claude/` | Claude Code | Claude settings と MCP 設定。 |
| `codex/` | Codex | Codex config と hook 登録。 |
| `copilot/` | GitHub Copilot CLI | Copilot settings と MCP 設定。 |
| `cursor/` | Cursor Agent | Cursor CLI config、MCP、hook 登録、`.cursorignore`。 |
| `devin/` | Devin CLI | Devin の local config と permission。 |
| `hermes-agent/` | Hermes Agent | Hermes config と追加 agent hook。 |
| `opencode/` | opencode | opencode config と JavaScript plugin。 |
| `openclaw/` | OpenClaw | OpenClaw workspace と MCP 設定。 |

## ファイル種別

- `settings.*`、`config.*`、`*.toml`、`*.yaml`、`*.json`: agent 固有 runtime 設定。
- `mcp*`: MCP server 定義。
- `hooks.json`: hook 対応 agent の hook 登録。
- 完了通知 hook: 対応 agent の turn 完了イベント（`Stop`、`agentStop`、`stop`、`post_llm_call`、opencode の `session.idle`）に `agent_turn_done_notify.sh` を登録します。Claude Code では idle / 権限通知用に `Notification` hook も残しますが、完了音は `Stop` が担当します。
- `plugins/`: plugin API を持つ agent 向けの plugin code / manifest。
- `agent-hooks/`: JSON hook map ではなく hook directory を読む agent 向けの shell hook。

## 更新ルール

- canonical agent 一覧は `../AGENT_SUPPORT.md` と揃えます。
- 新しい対応 agent を追加する場合は、このディレクトリ、`../AGENT_SUPPORT.md`、`../../scripts/setup_agent_files.sh`、関連テストを同じ変更で更新します。
- secret はこのディレクトリに置きません。runtime env file は `~/.config/shell/secrets.env` から生成します。
- 編集後は構造化 config を検証します。JSON は `jq empty`、shell hook は `bash -n`、利用可能なら agent 固有 validator を使います。

## よく使う確認コマンド

```bash
jq empty dotfiles/.agent/apps/claude/settings.json dotfiles/.agent/apps/copilot/settings.json dotfiles/.agent/apps/devin/config.json dotfiles/.agent/apps/codex/hooks.json dotfiles/.agent/apps/cursor/hooks.json dotfiles/.agent/apps/antigravity-cli/plugins/dotfiles-agent/hooks.json dotfiles/.agent/apps/opencode/opencode.json
bash -n dotfiles/.agent/hooks/agent_turn_done_notify.sh
bash -n dotfiles/.agent/apps/hermes-agent/agent-hooks/secret-protection.sh
zsh tests/test_agent_sync.sh
```
