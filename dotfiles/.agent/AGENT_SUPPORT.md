# Agent Support Matrix

このファイルは、dotfiles 内で Agent を直接呼び出すコードとツールの対応状況を管理するための一覧です。新しい Agent を追加する場合は、実装差分と同じ PR/作業でこの表と関連テストも更新してください。

## Canonical Agents

| Canonical ID | CLI / mise tool | 主な用途 |
|---|---|---|
| `claude` | `claude` / `claude-code` | Claude Code CLI |
| `codex` | `codex` / `codex` | Codex CLI |
| `copilot` | `copilot` / `npm:@github/copilot` | GitHub Copilot CLI |
| `cursor` | `cursor-agent` / `http:cursor-agent` | Cursor Agent |
| `devin` | `devin` / `http:devin` | Devin CLI |
| `antigravity` | `agy` / `brew cask: antigravity-cli` | Antigravity CLI |
| `hermes` | `hermes` / `pipx:git+https://github.com/NousResearch/hermes-agent.git` | Hermes Agent |
| `opencode` | `opencode` / `opencode` | opencode |
| `openclaw` | `openclaw` / `npm:openclaw` | OpenClaw |
| `grok` | `grok` / `npm:@xai-official/grok` | Grok CLI (xAI) |
| `agent-swarm` | `agent-swarm` / `npm:@desplega.ai/agent-swarm` | Desplega Agent Swarm CLI。非対話評価では `agent-swarm claude --headless -m` を使う。 |

## Internal Call Sites

| Code / tool | Supported agents | Notes |
|---|---|---|
| `scripts/agent_skill_upstreams.py` | `codex`, `claude-code`, `antigravity-cli`, `copilot`, `cursor-agent`, `devin`, `hermes`, `opencode`, `openclaw`, `grok`, `agent-swarm` | `--review-agent` で upstream skill 更新前の security review を実行する。 |
| `scripts/waza_eval_cli_agent.sh` | `codex`, `claude`, `antigravity`, `copilot`, `devin`, `cursor`, `opencode`, `hermes`, `openclaw`, `grok`, `agent-swarm`, `all` | Waza model suite を CLI agent で実行する public entrypoint。実装は `scripts/agent/waza_eval_cli_agent.sh`。`all` は全 canonical agent を対象にする。 |
| `dotfiles/.agent/skills/agent-job-scheduler/apps/agent-job-scheduler` | `antigravity`, `claude`, `codex`, `copilot`, `cursor`, `devin`, `hermes`, `opencode`, `openclaw`, `grok`, `agent-swarm` | CSV 台帳ベースの非対話ジョブスケジューラ。 |

## Checklist For Adding An Agent

- `config/mise/config.toml` と `home/.chezmoitemplates/mise-config.toml` に CLI 導入設定を追加する。
- `scripts/setup_agent_files.sh` と `tests/test_agent_sync.sh` に共通設定の同期を追加する。
- `scripts/agent_skill_upstreams.py` と `tests/test_agent_skill_upstreams.sh` に review agent として追加する。
- `scripts/agent/waza_eval_cli_agent.sh`、`scripts/waza_eval_cli_agent.sh`、`config/mise/config.toml`、`home/.chezmoitemplates/mise-config.toml`、`tests/test_nix_migration.sh` に Waza CLI eval 対応を追加する。
- `agent-job-scheduler` の `Agent` enum、adapter、rate limit profile、pytest、README / usage docs / sample assets を更新する。
- この `AGENT_SUPPORT.md` と `tests/test_agent_support_matrix.sh` を更新する。
