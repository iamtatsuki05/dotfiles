# AI Agent ファイル

English version: [README.md](README.md)

このディレクトリは、ローカル AI CLI agent 関連ファイルの source of truth です。

Agent CLI を内部で呼び出すコードやツールの対応状況は [AGENT_SUPPORT.md](AGENT_SUPPORT.md) にまとめています。対応 Agent を追加・削除する場合は、この matrix も更新してください。

管理対象:

- `codex`
- `claude-code`
- `copilot`
- `cursor-agent`
- `devin`
- `antigravity-cli`
- `hermes`
- `opencode`
- `openclaw`

CLI 本体は可能な範囲で `mise` から導入します。Antigravity CLI は Homebrew Cask `antigravity` として管理し、`agy` binary もそこから提供されます。このディレクトリでは prompt、agent 別設定、MCP、hooks、skills、Waza eval suite を管理します。

## 構成

- `AGENTS.md`: 共通 prompt。対応する tool home に symlink します。リポジトリルートには `AGENTS.md` symlink を置きません。
- `apps/`: agent 別の設定ファイル。詳細は [apps/README_JA.md](apps/README_JA.md) にまとめています。
- `hooks/`: `jupytext_sync.sh`、`agent_context_reminder.sh`、`agent_turn_done_notify.sh` などの共通 hook script。詳細は [hooks/README_JA.md](hooks/README_JA.md) にまとめています。
- `skills/`: Codex 互換 agent と Waza で使う共通 skill。階層、由来、各 skill の概要は [skills/README_JA.md](skills/README_JA.md) にまとめています。
- `evals/`: skill ごとの Waza eval suite。詳細は [evals/README_JA.md](evals/README_JA.md) にまとめています。
- `pets/`: packaged Codex pet asset。詳細は [pets/README_JA.md](pets/README_JA.md) にまとめています。
- `sync.sh`: `scripts/setup_agent_files.sh` への wrapper。
- `changes/`: 現在の作業メモ。ユーザー向けドキュメントではありません。

## 同期

`dotfiles/.agent/` 配下を変更したら次を実行します。

```bash
zsh dotfiles/.agent/sync.sh
```

`sync.sh` は `scripts/setup_agent_files.sh` を呼びます。各 tool home への symlink を作り、必要な agent 固有 env file を `~/.config/shell/secrets.env` から生成します。

## ファイル対応表

| Source | Destination |
|---|---|
| `AGENTS.md` | `~/.codex/AGENTS.md` |
| `AGENTS.md` | `~/.claude/CLAUDE.md` |
| `AGENTS.md` | `~/.copilot/copilot-instructions.md` |
| `AGENTS.md` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/rules/AGENTS.md` |
| `AGENTS.md` | `~/.cursor/AGENT.md` |
| `AGENTS.md` | `~/.config/opencode/AGENTS.md` |
| `AGENTS.md` | `~/.hermes/AGENTS.md` |
| `AGENTS.md` | `~/.openclaw/workspace/AGENTS.md` |
| `apps/claude/settings.json` | `~/.claude/settings.json` |
| `apps/claude/.mcp.json` | `~/.claude/.mcp.json` |
| `apps/copilot/settings.json` | `~/.copilot/settings.json` |
| `apps/copilot/mcp-config.json` | `~/.copilot/mcp-config.json` |
| `apps/codex/config.toml` | `~/.codex/config.toml` |
| `apps/codex/hooks.json` | `~/.codex/hooks.json` |
| `apps/cursor/cli-config.json` | `~/.cursor/cli-config.json` |
| `apps/cursor/hooks.json` | `~/.cursor/hooks.json` |
| `apps/cursor/mcp.json` | `~/.cursor/mcp.json` |
| `apps/devin/config.json` | `~/.config/devin/config.json` |
| `apps/antigravity-cli/settings.json` | `~/.gemini/antigravity-cli/settings.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/plugin.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/plugin.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/mcp_config.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/mcp_config.json` |
| `apps/antigravity-cli/plugins/dotfiles-agent/hooks.json` | `~/.gemini/antigravity-cli/plugins/dotfiles-agent/hooks.json` |
| `apps/hermes-agent/config.yaml` | `~/.hermes/config.yaml` |
| `apps/opencode/opencode.json` | `~/.config/opencode/opencode.json` |
| `apps/opencode/plugins/` | `~/.config/opencode/plugins/` |
| `apps/openclaw/openclaw.json` | `~/.openclaw/openclaw.json` |

`skills/` は各対応 agent の home に symlink します。Antigravity CLI では `~/.gemini/antigravity-cli/plugins/dotfiles-agent/skills` に symlink します。OpenClaw では `~/.openclaw/workspace/skills` に symlink します。共通 hook は `~/.claude/hooks/`、`~/.codex/hooks/`、`~/.copilot/hooks/`、`~/.cursor/hooks/`、`~/.config/devin/hooks/`、`~/.gemini/antigravity-cli/hooks/`、`~/.config/opencode/hooks/`、`~/.hermes/agent-hooks/` に symlink します。

Hermes では `apps/hermes-agent/agent-hooks/` のファイルも `~/.hermes/agent-hooks/` に symlink します。

`agent_context_reminder.sh` は、Claude Code、Codex、Copilot、Cursor、Devin、Antigravity CLI、Hermes の session / prompt 系 hook で同じリポジトリ向け reminder を注入します。opencode は Claude 型の prompt hook ではなく plugin event 方式のため、plugin 経由で compaction context に同じ hook 出力を入れます。OpenClaw は bundled internal hook の `bootstrap-extra-files` で、managed workspace の `AGENTS.md` を bootstrap context として読みます。

`agent_turn_done_notify.sh` は、Claude Code、Copilot、Cursor、Devin、Antigravity CLI、Hermes、opencode の対応する turn 完了イベントに登録します。Claude Code の完了通知は `Stop` hook を使います。`Notification` は権限要求または入力 idle 通知用です。Codex は native の `notify` 設定を使い続け、同じ共有 hook も再利用できるように `~/.codex/hooks/` へ symlink します。

## Ignore と secrets

project-level の除外は agent の機能に合わせて分けています。

- Cursor は repo root の `.cursorignore` を使います。実体は `apps/cursor/.cursorignore` です。
- Copilot は `respectGitignore` により `.gitignore` を使います。
- Devin は `respect_gitignore` と `apps/devin/config.json` の permission deny を使います。
- Codex、Claude、Antigravity CLI、opencode、Cursor、Devin、Hermes はそれぞれ app config 側で ignore または permission rule を持ちます。OpenClaw は workspace、skills、bootstrap hook、`mcp.servers` を共通設定に寄せています。ファイル単位の secret deny は、既存の共通 shell hook と OpenClaw の hook/policy 面が直接互換ではないため、現時点では移植していません。

secret はこのディレクトリには置かず、`~/.config/shell/secrets.env` に置きます。`sync.sh` は現在 `DEVIN_API_KEY` を次のファイルへ書き出します。

- `~/.gemini/antigravity-cli/.env`
- `~/.hermes/.env`

Waza の model suite は `copilot-sdk` executor を使うため、`GITHUB_TOKEN` が必要です。

## Jupyter Notebook

AI tool は `.ipynb` ではなく、ペアリングされた `.py` を編集します。対応する file edit 後に `hooks/jupytext_sync.sh` が実行され、ペアリング済み notebook を同期します。

新規 notebook をペアリングする場合:

```bash
jupytext --set-formats ipynb,py:percent notebook.py
```

## Waza

Waza は Nix の CLI package set に `dotfiles.waza` として含めています。

よく使うコマンド:

```bash
mise run waza-check
mise run waza-eval
mise run waza-eval-all
mise run waza-eval-model -- --allow
mise run waza-eval-model -- --agent all --dry-run
mise run waza-dashboard
```

model eval task を特定の CLI agent で実行する場合:

```bash
mise run waza-eval-model -- --agent codex --allow
mise run waza-eval-model -- --agent claude --allow
mise run waza-eval-model -- --agent antigravity --allow
mise run waza-eval-model -- --agent copilot --allow
mise run waza-eval-model -- --agent devin --allow
mise run waza-eval-model -- --agent cursor --allow
mise run waza-eval-model -- --agent opencode --allow
mise run waza-eval-model -- --agent hermes --allow
mise run waza-eval-model -- --agent openclaw --allow
```

AI CLI を起動せず対象 suite だけ確認する場合は `--dry-run` を使います。結果は `.waza-results/` に出力します。

## 外部 skill upstream

他人の skill を vendoring している場合は `skills/upstreams.json` で管理します。この manifest には upstream の GitHub repository、branch、固定 commit、local path、local tree hash を記録します。

よく使うコマンド:

```bash
python3 scripts/agent_skill_upstreams.py check
python3 scripts/agent_skill_upstreams.py updates
python3 scripts/agent_skill_upstreams.py update
mise run agent-skill-update
```

`update` は、デフォルトで登録済み upstream すべての最新 branch head を対象にします。review prompt を生成して選択した Agent を実行し、review report を `changes/skill-upstream-reviews/` に保存します。全 report が Critical / High finding なしで `update recommendation: approve` の場合だけ更新を適用します。

```bash
python3 scripts/agent_skill_upstreams.py update --dry-run
python3 scripts/agent_skill_upstreams.py update --review-agent antigravity-cli
python3 scripts/agent_skill_upstreams.py update --review-agent claude-code
python3 scripts/agent_skill_upstreams.py update --id superpowers --commit <40-char-sha>
```

review agent の default は `codex` です。選択できる agent は `codex`、`claude-code`、`antigravity-cli`、`copilot`、`cursor-agent`、`devin`、`hermes`、`opencode`、`openclaw` です。既定の review prompt は日本語で、`skills/review-prompts/skill-upstream-security.md` に置いています。別 prompt を使う場合は `--review-prompt <path>` を指定します。`update recommendation` などの report key は updater が読むため英語のままにしてください。

手動 review 用に、低レベルコマンドも残しています。

```bash
python3 scripts/agent_skill_upstreams.py security-prompt \
  --id superpowers \
  --review-agent codex \
  --commit <40-char-sha>
```

```bash
python3 scripts/agent_skill_upstreams.py apply-update \
  --id superpowers \
  --commit <40-char-sha> \
  --review-agent codex \
  --review-report dotfiles/.agent/changes/<review-report>.md \
  --security-reviewed
```

更新コマンドは vendored files、固定 commit、local tree hash、security review metadata を manifest に反映します。
